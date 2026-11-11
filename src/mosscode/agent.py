"""UI- and SDK-independent bounded ReAct runtime and event stream."""

import asyncio
import json
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import aclosing
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from .compaction import (
    CLEAR_TARGET,
    NOTHING,
    CompactionError,
    clear_results,
    clearable_tokens,
    compact_history,
    cut_point,
)
from .context import ContextBudget, ContextMeter
from .credentials import redact, redact_value
from .images import ImageRef
from .messages import (
    CONTINUE,
    NOTIFICATION,
    AutoContinue,
    CompactFailed,
    CompactFinished,
    CompactStarted,
    Completed,
    ContentBlock,
    IterationProgress,
    Message,
    ProviderError,
    ReplyRetry,
    ResponseBoundary,
    ResultsCleared,
    RunStopped,
    StreamEvent,
    TasksNotified,
    ToolDefinition,
    ToolProgress,
    ToolResult,
    Usage,
    UsageTotals,
    UsageUpdate,
    UserInput,
    message_id,
    user_content,
)
from .prompts import (
    PromptRequest,
    Supplement,
    active_skills,
    build_system_prompt,
    environment,
    hook_notification,
    mode_reminder,
    stable_prompt,
)
from .session import clear_record, compact_record, message_record, rewind_record, todo_record
from .skills import ActiveSkills
from .todo import todo_reminder
from .tools import shell_label
from .tools.base import ToolRegistry
from .tools.execution import execute_batch
from .usage import price_of, request_cost

if TYPE_CHECKING:
    from .catalog import ModelInfo


class Provider(Protocol):
    def stream(
        self,
        messages: Sequence[Message],
        system: str | PromptRequest,
        tools: Sequence[ToolDefinition] = (),
        *,
        allow_tools: bool = True,
    ) -> AsyncIterator[StreamEvent]: ...
    async def close(self) -> None: ...


class ResponseCollector:
    def __init__(self):
        self.response: Completed | None = None

    async def collect(self, stream):
        async with aclosing(stream) as events:
            async for event in events:
                if isinstance(event, Completed):
                    self.response = deepcopy(event)
                    return
                yield event
        raise ProviderError("响应在完成信号前中断；未执行不完整的工具调用。")


DEFAULT_MAX_ITERATIONS = 200

# A reply with neither text nor tool calls is dropped and the request sent again, at most this often.
REPLY_RETRIES = 2
REFUSALS = frozenset({"refusal", "content_filter"})
RETRY_REASONS = {"empty": "模型返回了空回复", "thinking_cut": "回复在思考阶段就达到了输出上限"}


# A reply cut off at the output limit after some text is continued automatically, at most this often per turn.
CONTINUATIONS = 3
CONTINUE_PROMPT = (
    "上一条回复达到输出上限，在中途被截断。请从中断处直接接着写：不要重复已经写出的内容，不要道歉或复述，"
    "也不要另起开头；剩余内容较多时，先写完能写完的部分，需要写入文件的长内容分多次写入。"
)


def truncation_notice(continued: int) -> str:
    done = f"已自动续写 {continued} 次，" if continued else ""
    return f"回复达到输出上限（max_output_tokens），{done}仍被截断；可以输入“继续”或调高该配置。"


def unusable(final: Completed) -> str | None:
    """Why a complete reply cannot go into the history, or None when it can."""
    message = final.message
    if message.tool_calls or any(b.kind != "thinking" and b.text.strip() for b in message.content):
        return None
    if final.stop_reason in REFUSALS:
        return "refusal"
    return "thinking_cut" if final.stop_reason == "max_tokens" else "empty"


def recovery_note(kind: str, request: Sequence[Message]) -> Supplement:
    """Sent with the repeated request only: never part of the history or the cached prefix."""
    if kind == "thinking_cut":
        text = (
            "上一次回复在写出任何正文或工具调用之前就达到了输出上限（多半是思考过长），已丢弃。"
            "请缩短思考，直接给出回复或下一步的工具调用。"
        )
    elif request and request[-1].role == "tool":
        text = "上一次回复是空的（没有正文，也没有工具调用），已丢弃。请根据上方的工具结果继续：给出回复，或调用工具进行下一步。"
    else:
        text = "上一次回复是空的（没有正文，也没有工具调用），已丢弃。请继续：给出回复，或调用工具进行下一步。"
    return Supplement("reply-recovery", text)


def unusable_notice(kind: str, final: Completed, attempts: int) -> str:
    """Enough to tell a service problem from a model problem."""
    stop = final.stop_reason or "未知"
    if kind == "refusal":
        return f"服务拒绝了这次请求，没有返回内容（停止原因：{stop}）；可以换个说法重新发送。"
    kinds = "、".join(dict.fromkeys(b.kind if b.text.strip() else f"空 {b.kind}" for b in final.message.content))
    notice = f"模型连续 {attempts} 次没有返回可用的内容（停止原因：{stop}；内容块：{kinds or '无'}），已停止。"
    if kind == "thinking_cut":
        return notice + "回复在思考阶段就达到了输出上限：可以调高 max_output_tokens 或降低思考强度后重新发送。"
    return notice + "可以重新发送；反复出现时检查模型或服务的状态。"


def add_usage(total: UsageTotals, usage: Usage, price: "ModelInfo | None" = None) -> UsageTotals:
    cost = request_cost(usage, price)
    return UsageTotals(
        input_tokens=total.input_tokens + (usage.input_tokens or 0),
        output_tokens=total.output_tokens + (usage.output_tokens or 0),
        missing_input=total.missing_input + (usage.input_tokens is None),
        missing_output=total.missing_output + (usage.output_tokens is None),
        requests=total.requests + 1,
        cached_input_tokens=total.cached_input_tokens + (usage.cached_input_tokens or 0),
        cache_write_tokens=total.cache_write_tokens + (usage.cache_write_tokens or 0),
        missing_cached=total.missing_cached + (usage.cached_input_tokens is None),
        missing_cache_write=total.missing_cache_write + (usage.cache_write_tokens is None),
        cost_usd=total.cost_usd + (cost or 0.0),
        missing_cost=total.missing_cost + (cost is None),
    )


def _redacted(message: Message) -> Message:
    """message with any known API key hidden: attachments, ! commands and hook context reach the model this way."""
    content = tuple(
        replace(block, text=redact(block.text)) if block.kind == "text" else block for block in message.content
    )
    results = tuple(
        replace(result, output=redact(result.output), details=redact_value(result.details))
        for result in message.tool_results
    )
    if content == message.content and results == message.tool_results:
        return message
    return replace(message, content=content, tool_results=results)


BREAKER = 3  # Consecutive automatic compaction failures before automatic compaction pauses.


class AgentLoop:
    def __init__(
        self,
        provider: Provider,
        system: str,
        registry: ToolRegistry | None,
        *,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        unknown_limit: int = 3,
    ):
        if type(max_iterations) is not int or max_iterations < 1 or type(unknown_limit) is not int or unknown_limit < 1:
            raise ValueError("循环上限与未知工具阈值必须是正整数。")
        self.provider, self.system, self.registry = provider, system, registry
        self.system = stable_prompt(system)
        self.max_iterations, self.unknown_limit = max_iterations, unknown_limit
        # Assembled by the application layer (project files, git); the loop never reads them itself.
        self.instructions = ""
        self.git: dict | None = None
        self.skill_catalog = ""  # The model-visible list of skills, after the instructions.
        self.agent_catalog = ""  # The sub-agent types, after the skills.
        self.team_guide = ""  # The lead's team section; never copied to forks or members.
        self.active_skills = ActiveSkills()  # Pinned SOPs, sent in the cached prefix.
        self._announced = "normal"
        self.history: list[Message] = []
        self.busy = False
        self.total_usage = UsageTotals()
        # Steering: text queued while running, merged before the next model request.
        self.pending: list[str] = []
        self.pending_images: dict[str, ImageRef] = {}  # By placeholder, for the queued texts.
        # Background task notifications, merged the same way as their own message.
        self.notifications: list[str] = []
        self.on_usage = None  # A sub-agent reports each request's usage to its task and the session.
        # Context management: thresholds of the current provider, set by the application.
        self.budget = ContextBudget.for_provider(None, 0)
        self.session = None
        self.auto_compact = True
        self.meter = ContextMeter()
        self.compact_failures = 0
        self.compactions = 0
        self._reannounce = False
        # Set by the application (the session transcript): receives each record as history changes.
        self.on_commit = None
        # Set by the memory extractor: messages a compaction is about to replace with a summary.
        self.on_compact = None
        self._gap: Supplement | None = None  # One-time reminder after resuming an old session or a /rewind.
        # The main conversation's file checkpoints: each typed message starts a new turn of snapshots.
        self.checkpoints = None
        # The main conversation's HookEngine (a fork sub-agent has none).
        self.hooks = None
        self.hook_context: Supplement | None = None  # session_start output, cached prefix.
        self.turn_notes: tuple[tuple[str, str], ...] = ()  # turn_start output, every request this turn.
        self.goal_note: Supplement | None = None  # The /goal condition, set by the conversation.
        if registry is not None:
            registry.todos.on_change = lambda items, mark: self._commit(todo_record(items, mark))

    def _commit(self, record: dict) -> None:
        if self.on_commit is not None:
            self.on_commit(record)

    def _append(self, message: Message) -> None:
        if message.role == "user" and message.synthetic is None and message.id is None:
            message = replace(message, id=message_id())
        if message.role != "assistant":
            message = _redacted(message)
        if message.id is not None and self.checkpoints is not None:
            self.checkpoints.begin(message.id)
        self.history.append(message)
        self._commit(message_record(message))

    def truncate(self, keep: int, message: str) -> None:
        """/rewind: the history before index keep. Usage stays spent; skills and the session's hook context stay."""
        if self.busy:
            raise ProviderError("当前任务正在运行，不能回退。")
        self.history = self.history[:keep]
        self.meter.invalidate()
        self.compact_failures = 0
        if self.registry is not None:
            self.registry.forget_reads()
            self.registry.todos.rewind(keep)
        self._commit(rewind_record(keep, message))

    def remind(self, reminder: Supplement) -> None:
        """Sent once with the next request, never stored in the history."""
        self._gap = reminder

    def restore(self, history: list[Message], gap: Supplement | None = None):
        """Resume a saved session: its messages only; usage, reads and the mode announcement start afresh."""
        if self.busy:
            raise ProviderError("当前任务正在运行，不能恢复会话。")
        self.clear()
        self.history = list(history)
        if self.registry is not None:
            self.registry.forget_reads()
        self._gap = gap

    @property
    def mode(self):
        # Plan is one of the permission modes; the policy holds the only copy.
        return "plan" if self.registry is not None and self.registry.permissions.mode == "plan" else "normal"

    def set_mode(self, mode: str):
        if mode not in {"normal", "plan"}:
            raise ValueError("模式必须为 normal 或 plan。")
        if self.busy:
            raise ProviderError("当前任务正在运行，不能切换模式。")
        if self.registry is None:
            if mode == "plan":
                raise ProviderError("当前会话未启用工具，不能进入计划模式。")
            return
        policy = self.registry.permissions
        if mode == "plan":
            policy.set_mode("plan")
        elif policy.mode == "plan":
            policy.set_mode(policy.resume_mode)

    def clear(self):
        if self.busy:
            raise ProviderError("当前任务正在运行，不能清空会话。")
        self.history, self.pending, self.notifications = [], [], []
        self.pending_images = {}
        self.total_usage = UsageTotals()
        # A fresh context has not seen the mode rules yet.
        self._announced = "normal"
        self.meter.invalidate()
        self.compact_failures = 0
        self._gap = None
        self.active_skills.clear()
        self.hook_context, self.turn_notes = None, ()  # The next session_start regenerates it.
        if self.registry is not None:
            self.registry.todos.clear()

    def set_provider(self, provider: Provider, budget: ContextBudget | None = None):
        if self.busy:
            raise ProviderError("当前任务正在运行，不能切换模型。")
        self.provider = provider
        if budget is not None:
            self.budget = budget
        # Another model reports usage differently; start from a full estimate and a fresh breaker.
        self.meter.invalidate()
        self.compact_failures = 0
        # Signed/opaque blocks are valid only for the model that produced them.
        self.history = [replace(m, protocol=None, native=()) for m in self.history]

    def _definitions(self):
        return self.registry.definitions(plan=self.mode == "plan") if self.registry else ()

    def _prompt(self, definitions):
        instructions = "\n\n".join(
            part for part in (self.instructions, self.skill_catalog, self.agent_catalog, self.team_guide) if part
        )
        return build_system_prompt(definitions, override=self.system, instructions=instructions)

    def _active(self) -> Supplement | None:
        return active_skills(self.active_skills.texts())

    def _model(self, messages, iteration, env, notes=(), recovery: Supplement | None = None):
        reminder = mode_reminder(self.mode, self._announced) if iteration == 1 or self._reannounce else None
        self._announced, self._reannounce = self.mode, False
        definitions = self._definitions()
        todos = todo_reminder(self.registry.todos if self.registry is not None else None)
        extra = tuple(
            item
            for item in (
                reminder,
                self._gap,
                todos,
                self.goal_note,
                hook_notification((*self.turn_notes, *notes)),
                recovery,
            )
            if item
        )
        self._gap = None  # Never part of the history or the transcript; sent with the first request only.
        prefix = tuple(item for item in (env, self._active(), self.hook_context) if item)
        prompt = PromptRequest(self._prompt(definitions), prefix + extra)
        if self.registry is None:
            return self.provider.stream(messages, prompt)
        return self.provider.stream(messages, prompt, definitions)

    def _fixed_text(self) -> str:
        """System prompt and tool definitions, for a full estimate when no usage anchors it."""
        definitions = self._definitions()
        schemas = json.dumps([asdict(d) for d in definitions], ensure_ascii=False)
        return self._prompt(definitions) + schemas

    def estimate(self, extra: Sequence[Message] = (), env_text: str = "") -> int:
        active = self._active()
        env_text += active.render() if active else ""
        env_text += self.hook_context.render() if self.hook_context else ""
        return self.meter.estimate(list(self.history) + list(extra), env_text, self._fixed_text)

    def _clear(self, cut: int, estimate: int) -> ResultsCleared | None:
        """Older tool results become notes when that alone brings the context far enough down.

        Done only when compaction is due anyway: the cached prefix is about to be rewritten either way,
        and unlike a summary this costs no request and keeps every word of the conversation.
        """
        target = int(self.budget.auto_threshold * CLEAR_TARGET)
        if estimate - clearable_tokens(self.history, cut) > target:
            return None
        done = clear_results(self.history, cut, self.session)
        if not done.results or estimate - done.tokens > target:
            return None  # Too few could be kept on disk; the history stays as it was.
        self.history = done.history
        self._commit(clear_record(done.results))
        if self.registry is not None:
            self.registry.forget_reads(done.paths)
        self.meter.invalidate()
        return ResultsCleared(len(done.results), done.tokens)

    async def _compact(
        self,
        instructions: str = "",
        *,
        manual: bool = False,
        failure: str | None = None,
        estimate: int | None = None,
    ):
        """One compaction attempt; events report the outcome, the history changes only on success.

        With the estimate that made it due, clearing older tool results is tried before summarizing.
        """
        cut = cut_point(self.history, self.budget.auto_threshold, manual=manual)
        if not cut:
            # Nothing older than the kept tail: automatic attempts skip silently and never trip the breaker.
            if manual:
                yield CompactFailed(NOTHING, self.compact_failures, self.auto_paused, "nothing", True)
            return
        if estimate is not None:
            cleared = self._clear(cut, estimate)
            if cleared is not None:
                yield cleared
                return
        yield CompactStarted(manual)
        started = time.monotonic()
        if self.on_compact is not None:
            # Messages about to be summarized away; the memory extractor still gets to read them.
            self.on_compact(self.history[:cut])
        try:
            done = await compact_history(
                self.provider, self.history, self.budget, session=self.session, instructions=instructions, manual=manual
            )
        except CompactionError as exc:
            if failure is not None:  # Resuming: its own message, and the breaker is untouched.
                yield CompactFailed(failure.format(exc), self.compact_failures, self.auto_paused, "restore")
                return
            if not manual and exc.code != "nothing":
                self.compact_failures += 1
            yield CompactFailed(str(exc), self.compact_failures, self.auto_paused, exc.code, manual)
            return
        self.history = done.history
        self._commit(compact_record(done.history[0], len(done.history) - 1, str(done.record) if done.record else None))
        if self.registry is not None:
            self.registry.forget_reads()
            self.registry.todos.rebase()
        # The mode rules may have been summarized away; the next request repeats them (as after /clear).
        self._announced, self._reannounce = "normal", True
        self.meter.invalidate()
        self.compact_failures = 0
        self.compactions += 1
        yield CompactFinished(
            done.removed, done.summary_tokens, time.monotonic() - started, str(done.record) if done.record else None
        )
        if self.hooks is not None and self.hooks.wants("compact"):
            from .hooks import HookEvent

            trigger = "restore" if failure is not None else "manual" if manual else "auto"
            await self.hooks.fire(HookEvent("compact", fields={"trigger": trigger}))

    async def compact(self, instructions: str = ""):
        """Manual /compact: idle only, regardless of the threshold; resets the breaker on success."""
        if self.busy:
            raise ProviderError("当前任务正在运行，不能压缩上下文。")
        self.busy = True
        try:
            async with aclosing(self._compact(instructions, manual=True)) as events:
                async for event in events:
                    yield event
        finally:
            self.busy = False

    async def compact_restored(self):
        """A resumed session over the threshold is compacted before the first input, even with
        auto_compact off; like /compact it never trips the breaker."""
        if self.busy:
            raise ProviderError("当前任务正在运行，不能压缩上下文。")
        if self.estimate() < self.budget.auto_threshold:
            return
        self.busy = True
        try:
            failure = "恢复的会话超出上下文窗口，压缩失败：{}；可以 /compact 重试或 /clear"
            async with aclosing(self._compact(failure=failure)) as events:
                async for event in events:
                    yield event
        finally:
            self.busy = False

    @property
    def auto_paused(self) -> bool:
        return self.compact_failures >= BREAKER

    def _may_compact(self) -> bool:
        return self.auto_compact and not self.auto_paused

    def _usage(self, usage):
        self.total_usage = add_usage(self.total_usage, usage, price_of(self.provider))
        if self.on_usage is not None:
            self.on_usage(usage)

    async def _hook(self, name: str, fields: dict) -> tuple[tuple[str, str], ...]:
        if self.hooks is None or not self.hooks.wants(name):
            return ()
        from .hooks import HookEvent

        return (await self.hooks.fire(HookEvent(name, fields=fields))).notes

    async def run(
        self,
        prompt: str,
        *,
        command: str | None = None,
        synthetic: str | None = None,
        images: Mapping[str, ImageRef] | None = None,
        attachments: Message | None = None,
    ):
        """images: attached pictures by their placeholder in prompt, e.g. "[图片 #1]".

        attachments: what the prompt's @ mentions hold, sent and kept right after it.
        """
        if self.busy:
            raise ProviderError("已有请求正在运行，请等待或取消。")
        if not prompt.strip():
            raise ProviderError("请输入非空消息。")
        self.busy = True
        iteration, unknown_run = 0, 0
        user = _redacted(Message("user", user_content(prompt, images), command=command, synthetic=synthetic))
        attachments = None if attachments is None else _redacted(attachments)
        committed = False
        continued, before = 0, ""

        def outcomes():
            return self.history[-1].tool_results if committed and self.history[-1].role == "tool" else ()

        try:
            definitions = self.registry.definitions() if self.registry else ()
            root = self.registry.get(definitions[0].name).root if definitions else Path.cwd()
            for iteration in range(1, self.max_iterations + 1):
                # Rebuilt each round: approving a plan changes the mode mid-task.
                env = environment(
                    root,
                    tools_enabled=self.registry is not None,
                    permission_mode=self.registry.permissions.mode if self.registry else None,
                    git=self.git,
                    shell=shell_label(self.registry),
                )
                yield IterationProgress(iteration, self.max_iterations, self.mode)
                if committed and self.pending:
                    texts, self.pending = tuple(self.pending), []
                    images, self.pending_images = self.pending_images, {}
                    self._append(Message("user", user_content("\n\n".join(texts), images)))
                    yield UserInput(texts)
                if committed and self.notifications:
                    texts, self.notifications = tuple(self.notifications), []
                    self._append(Message("user", (ContentBlock("text", "\n\n".join(texts)),), synthetic=NOTIFICATION))
                    yield TasksNotified(texts)
                fresh = [] if committed else [user, *([attachments] if attachments else [])]
                size = self.estimate(fresh, env.text) if self._may_compact() else 0
                if self._may_compact() and size >= self.budget.auto_threshold:
                    async with aclosing(self._compact(estimate=size)) as events:
                        async for event in events:
                            yield event
                notes = await self._hook("pre_send", {"iteration": str(iteration)})
                rescued, dropped, recovery = False, 0, None
                while True:
                    request = deepcopy(self.history) + fresh
                    collector = ResponseCollector()
                    # One-time reminders are consumed by _model; a dropped reply sends them again.
                    once = (self._announced, self._reannounce, self._gap)
                    try:
                        model = self._model(request, iteration, env, notes, recovery)
                        async with aclosing(collector.collect(model)) as events:
                            async for event in events:
                                yield event
                    except ProviderError as exc:
                        # Over the window despite the estimate. Compact once, retry once, never loop.
                        if exc.code != "context_overflow" or rescued or not self._may_compact():
                            raise
                        rescued, compacted = True, False
                        async with aclosing(self._compact()) as events:
                            async for event in events:
                                compacted = compacted or isinstance(event, CompactFinished)
                                yield event
                        if not compacted:
                            raise exc
                        continue
                    final = collector.response
                    kind = None if final is None or final.message.role != "assistant" else unusable(final)
                    if kind is not None:
                        self._usage(final.usage)  # Dropped, but its tokens were spent.
                        if kind == "refusal" or dropped >= REPLY_RETRIES:
                            raise ProviderError(unusable_notice(kind, final, dropped + 1), code="empty_reply")
                        dropped += 1
                        self._announced, self._reannounce, self._gap = once
                        recovery = recovery_note(kind, request)
                        yield ReplyRetry(RETRY_REASONS[kind], dropped, REPLY_RETRIES)
                        continue
                    break
                final = collector.response
                if final is None or final.message.role != "assistant":
                    raise ProviderError("服务返回了不合法的回复。")
                calls = final.message.tool_calls
                ids = [call.id for call in calls]
                if any(not call.id or not call.name for call in calls) or len(ids) != len(set(ids)):
                    raise ProviderError("工具调用 ID 或名称不合法；未执行工具。")
                if calls and self.registry is None:
                    raise ProviderError("当前会话未启用工具。")
                await self._hook("post_receive", {"iteration": str(iteration), "tool_calls": str(len(calls))})
                if not committed:
                    self._append(deepcopy(user))
                    if attachments is not None:
                        self._append(deepcopy(attachments))
                    committed = True
                self._append(deepcopy(final.message))
                self._usage(final.usage)
                self.meter.record(final.usage, len(self.history))
                truncated = final.stop_reason == "max_tokens"
                if not calls:
                    yield UsageUpdate(iteration, final.usage, self.total_usage)
                    yield replace(final, before=before) if before else final
                    if truncated and continued < CONTINUATIONS:
                        continued += 1
                        before += final.message.text
                        self._append(Message("user", (ContentBlock("text", CONTINUE_PROMPT),), synthetic=CONTINUE))
                        yield AutoContinue(continued, CONTINUATIONS)
                        continue
                    if truncated:
                        yield RunStopped("max_tokens", truncation_notice(continued), iteration)
                    else:
                        yield RunStopped("completed", "", iteration)
                    return
                if truncated:
                    # Arguments may be cut mid-JSON: never execute them, let the model retry smaller.
                    results = tuple(
                        ToolResult(
                            c.id,
                            c.name,
                            "回复达到输出上限，此调用的参数可能不完整，未执行。"
                            "请缩小单次输出后重试，例如分多次写入或编辑。",
                            True,
                            "output_truncated",
                            {"executed": False},
                        )
                        for c in calls
                    )
                    self._append(Message("tool", (), tool_results=results))
                    yield UsageUpdate(iteration, final.usage, self.total_usage)
                    yield ResponseBoundary(final.message)
                    for call, result in zip(calls, results):
                        yield ToolProgress(call, "finished", result)
                    continue
                # Enter paired-results cleanup before yielding a committed call.
                position = len(self.history)
                try:
                    # Read per batch: /model may have switched to a model with or without image input.
                    self.registry.images = getattr(getattr(self.provider, "config", None), "accepts_images", True)
                    # Checklist changes in this batch are undone by a /rewind that drops this response.
                    self.registry.todos.mark = len(self.history)
                    async with aclosing(execute_batch(final.message, self.registry, self.history)) as batch:
                        boundary = await anext(batch)
                        yield UsageUpdate(iteration, final.usage, self.total_usage)
                        yield boundary
                        async for event in batch:
                            yield event
                finally:
                    # The batch's final results, once, also when cancelled: never its intermediate states.
                    if len(self.history) > position:
                        self._commit(message_record(self.history[position]))
                unknown_run = unknown_run + 1 if all(self.registry.get(c.name) is None for c in calls) else 0
                if unknown_run >= self.unknown_limit:
                    yield RunStopped(
                        "unknown_tools",
                        f"连续 {self.unknown_limit} 轮仅请求未知工具，已停止；可以修改任务后继续。",
                        iteration,
                        outcomes(),
                    )
                    return
            yield RunStopped(
                "max_iterations",
                f"已达最大迭代轮数 {self.max_iterations}，已停止；可以继续提问推进任务。",
                iteration,
                outcomes(),
            )
        except asyncio.CancelledError:
            yield RunStopped("cancelled", "已取消；已执行工具及其结果保留。", iteration, outcomes())
            raise
        except ProviderError as exc:
            yield RunStopped("error", str(exc), iteration, outcomes())
            raise
        except Exception:
            notice = "Agent 运行出现内部错误；已执行工具及其结果保留。"
            yield RunStopped("error", notice, iteration, outcomes())
            raise ProviderError(notice) from None
        finally:
            self.busy = False
