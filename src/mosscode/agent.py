"""UI- and SDK-independent bounded ReAct runtime and event stream."""

import asyncio
from contextlib import aclosing
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import AsyncIterator, Protocol, Sequence

from .messages import (Completed, Message, ContentBlock, ProviderError, StreamEvent, ResponseBoundary, ToolDefinition,
                       ToolProgress, ToolResult, IterationProgress, UsageTotals, UsageUpdate, RunStopped, UserInput)
from .tools import shell_label
from .tools.base import ToolRegistry
from .tools.execution import execute_batch
from .prompts import PromptRequest, build_system_prompt, environment, mode_reminder, stable_prompt


class Provider(Protocol):
    def stream(self, messages: Sequence[Message], system: str | PromptRequest, tools: Sequence[ToolDefinition] = (),
               *, allow_tools: bool = True) -> AsyncIterator[StreamEvent]: ...
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


class AgentLoop:
    def __init__(self, provider: Provider, system: str, registry: ToolRegistry | None,
                 *, max_iterations: int = DEFAULT_MAX_ITERATIONS, unknown_limit: int = 3):
        if type(max_iterations) is not int or max_iterations < 1 or type(unknown_limit) is not int or unknown_limit < 1:
            raise ValueError("循环上限与未知工具阈值必须是正整数。")
        self.provider, self.system, self.registry = provider, system, registry
        self.system = stable_prompt(system)
        self.max_iterations, self.unknown_limit = max_iterations, unknown_limit
        # Assembled by the application layer (project files, git); the loop never reads them itself.
        self.instructions = ""
        self.git: dict | None = None
        self._announced = "normal"
        self.history: list[Message] = []
        self.busy = False
        self.total_usage = UsageTotals()
        # Steering: text queued while running, merged before the next model request.
        self.pending: list[str] = []

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
        self.history, self.pending = [], []
        self.total_usage = UsageTotals()
        # A fresh context has not seen the mode rules yet.
        self._announced = "normal"

    def set_provider(self, provider: Provider):
        if self.busy:
            raise ProviderError("当前任务正在运行，不能切换模型。")
        self.provider = provider
        # Signed/opaque blocks are valid only for the model that produced them.
        self.history = [replace(m, protocol=None, native=()) for m in self.history]

    def _model(self, messages, iteration, env):
        reminder = mode_reminder(self.mode, self._announced) if iteration == 1 else None
        self._announced = self.mode
        definitions = self.registry.definitions(plan=self.mode == "plan") if self.registry else ()
        prompt = PromptRequest(build_system_prompt(definitions, override=self.system, instructions=self.instructions),
                               (env,) + ((reminder,) if reminder else ()))
        if self.registry is None:
            return self.provider.stream(messages, prompt)
        return self.provider.stream(messages, prompt, definitions)

    def _usage(self, usage):
        total = self.total_usage
        self.total_usage = UsageTotals(
            input_tokens=total.input_tokens + (usage.input_tokens or 0),
            output_tokens=total.output_tokens + (usage.output_tokens or 0),
            missing_input=total.missing_input + (usage.input_tokens is None),
            missing_output=total.missing_output + (usage.output_tokens is None), requests=total.requests + 1,
            cached_input_tokens=total.cached_input_tokens + (usage.cached_input_tokens or 0),
            cache_write_tokens=total.cache_write_tokens + (usage.cache_write_tokens or 0),
            missing_cached=total.missing_cached + (usage.cached_input_tokens is None),
            missing_cache_write=total.missing_cache_write + (usage.cache_write_tokens is None))

    async def run(self, prompt: str):
        if self.busy:
            raise ProviderError("已有请求正在运行，请等待或取消。")
        if not prompt.strip():
            raise ProviderError("请输入非空消息。")
        self.busy = True
        iteration, unknown_run = 0, 0
        user = Message("user", (ContentBlock("text", prompt),))
        committed = False
        def outcomes():
            return self.history[-1].tool_results if committed and self.history[-1].role == "tool" else ()
        try:
            definitions = self.registry.definitions() if self.registry else ()
            root = self.registry.get(definitions[0].name).root if definitions else Path.cwd()
            for iteration in range(1, self.max_iterations + 1):
                # Rebuilt each round: approving a plan changes the mode mid-task.
                env = environment(root, tools_enabled=self.registry is not None,
                                  permission_mode=self.registry.permissions.mode if self.registry else None, git=self.git,
                                  shell=shell_label(self.registry))
                yield IterationProgress(iteration, self.max_iterations, self.mode)
                if committed and self.pending:
                    texts, self.pending = tuple(self.pending), []
                    self.history.append(Message("user", (ContentBlock("text", "\n\n".join(texts)),)))
                    yield UserInput(texts)
                request = deepcopy(self.history) + ([] if committed else [user])
                collector = ResponseCollector()
                async with aclosing(collector.collect(self._model(request, iteration, env))) as events:
                    async for event in events:
                        yield event
                final = collector.response
                if final is None or final.message.role != "assistant":
                    raise ProviderError("服务返回了不合法的回复。")
                calls = final.message.tool_calls
                ids = [call.id for call in calls]
                if any(not call.id or not call.name for call in calls) or len(ids) != len(set(ids)):
                    raise ProviderError("工具调用 ID 或名称不合法；未执行工具。")
                if calls and self.registry is None:
                    raise ProviderError("当前会话未启用工具。")
                if not committed:
                    self.history.append(deepcopy(user))
                    committed = True
                self.history.append(deepcopy(final.message))
                self._usage(final.usage)
                truncated = final.stop_reason == "max_tokens"
                if not calls:
                    yield UsageUpdate(iteration, final.usage, self.total_usage)
                    yield final
                    if truncated:
                        yield RunStopped("max_tokens", "回复达到输出上限（max_output_tokens），已截断；可以输入“继续”或调高该配置。", iteration)
                    else:
                        yield RunStopped("completed", "", iteration)
                    return
                if truncated:
                    # Arguments may be cut mid-JSON: never execute them, let the model retry smaller.
                    results = tuple(ToolResult(c.id, c.name, "回复达到输出上限，此调用的参数可能不完整，未执行。"
                                               "请缩小单次输出后重试，例如分多次写入或编辑。", True, "output_truncated",
                                               {"executed": False}) for c in calls)
                    self.history.append(Message("tool", (), tool_results=results))
                    yield UsageUpdate(iteration, final.usage, self.total_usage)
                    yield ResponseBoundary(final.message)
                    for call, result in zip(calls, results):
                        yield ToolProgress(call, "finished", result)
                    continue
                # Enter paired-results cleanup before yielding a committed call.
                async with aclosing(execute_batch(final.message, self.registry, self.history)) as batch:
                    boundary = await anext(batch)
                    yield UsageUpdate(iteration, final.usage, self.total_usage)
                    yield boundary
                    async for event in batch:
                        yield event
                unknown_run = unknown_run + 1 if all(self.registry.get(c.name) is None for c in calls) else 0
                if unknown_run >= self.unknown_limit:
                    yield RunStopped("unknown_tools", f"连续 {self.unknown_limit} 轮仅请求未知工具，已停止；可以修改任务后继续。", iteration, outcomes())
                    return
            yield RunStopped("max_iterations", f"已达最大迭代轮数 {self.max_iterations}，已停止；可以继续提问推进任务。", iteration, outcomes())
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
