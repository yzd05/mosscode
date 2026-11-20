"""Skill lifecycle, transcript replay, and forked skill execution for Conversation."""

from __future__ import annotations

import asyncio
import difflib
import json
from contextlib import aclosing
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from .agent import AgentLoop
from .agents.runtime import GLOBAL_BAN
from .checkpoints import parse_active_skills
from .messages import Completed, ContentBlock, Message, ProviderError, RunStopped, ToolProgress, UsageUpdate
from .session import skill_record
from .skills import ACTIVE_LIMIT, LOAD_SKILL, ForkRequest, SkillCatalog, SkillDef, SkillError, missing_tools
from .tools.base import ToolFailure, ToolRegistry
from .tools.skill import LoadSkill, SkillTool


def _replay_skill_state(
    path: Path,
) -> tuple[dict[str, tuple[tuple[str, str], ...]], tuple[tuple[str, str, bool], ...] | None]:
    """Reconstruct per-message skill snapshots and post-rewind/unload effective skills from a transcript."""
    pinned: dict[str, str] = {}
    lru: dict[str, None] = {}
    inline_used: list[tuple[str, str]] = []
    skill_marks: dict[str, tuple[tuple[str, str], ...]] = {}
    lru_marks: dict[str, tuple[str, ...]] = {}
    inline_marks: dict[str, int] = {}
    mutated = False
    try:
        with path.open("rb") as stream:
            for raw in stream:
                if not raw.strip():
                    continue
                try:
                    record = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                kind = record.get("type")
                if kind == "message":
                    msg = record.get("message")
                    if (
                        isinstance(msg, dict)
                        and msg.get("role") == "user"
                        and msg.get("synthetic") is None
                        and isinstance(msg.get("id"), str)
                    ):
                        msg_id = msg["id"]
                        skill_marks[msg_id] = tuple(pinned.items())
                        lru_marks[msg_id] = tuple(lru)
                        inline_marks[msg_id] = len(inline_used)
                elif kind == "checkpoint" and isinstance(record.get("message"), str) and "active_skills" in record:
                    parsed = parse_active_skills(record.get("active_skills"))
                    skill_marks.setdefault(record["message"], parsed)
                    lru_marks.setdefault(record["message"], tuple(name for name, _ in parsed))
                elif kind == "skill":
                    name, arguments, is_pinned = record.get("name"), record.get("arguments", ""), record.get("pinned")
                    if isinstance(name, str) and name and isinstance(arguments, str) and isinstance(is_pinned, bool):
                        if is_pinned:
                            if name in pinned:
                                mutated = True
                            pinned[name] = arguments
                            lru.pop(name, None)
                            lru[name] = None
                        else:
                            inline_used.append((name, arguments))
                elif kind == "skill_unload":
                    name = record.get("name")
                    if isinstance(name, str) and name:
                        mutated = True
                        pinned.pop(name, None)
                        lru.pop(name, None)
                elif kind == "rewind":
                    msg_id = record.get("message")
                    if isinstance(msg_id, str) and msg_id in skill_marks:
                        mutated = True
                        pinned = dict(skill_marks[msg_id])
                        saved_lru = lru_marks.get(msg_id, tuple(pinned))
                        lru = {name: None for name in saved_lru if name in pinned}
                        for name in pinned:
                            lru.setdefault(name, None)
                        inline_used = inline_used[: inline_marks.get(msg_id, len(inline_used))]
    except OSError:
        return {}, None
    if not mutated:
        return skill_marks, None
    recency_tail = (
        tuple((name, pinned[name], True) for name in lru if name in pinned) if tuple(lru) != tuple(pinned) else ()
    )
    effective = tuple(
        [
            *((name, args, False) for name, args in inline_used),
            *((name, args, True) for name, args in pinned.items()),
            *recency_tail,
        ]
    )
    return skill_marks, effective


class _ConversationSkillsMixin:
    """Active skill management, checkpoint replay, and forked skill execution."""

    # Skills ---------------------------------------------------------------------------------

    @property
    def active_skills(self) -> tuple[str, ...]:
        return self._agent.active_skills.names

    def _offer_skills(self) -> list[str]:
        """The catalog section and load_skill, when tools are on and the model may load a skill."""
        if self.registry is None:
            return []
        section, notice = self.skills.catalog_section()
        self._agent.skill_catalog = section
        if section and self.registry.get(LOAD_SKILL) is None:
            self.registry.register(LoadSkill(self.registry.permissions.root, self._load_skill, self._unload_skill_tool))
        elif not section and self.registry.get(LOAD_SKILL) is not None:
            self.registry.unregister(LOAD_SKILL)
        return [notice] if notice else []

    def _missing(self, skill: SkillDef) -> list[str]:
        """The tools it lists must be registered now."""
        return missing_tools(skill, self.registry.names() if self.registry is not None else ())

    def _use_skill_dir(self, skill: SkillDef, *, inline: bool = False) -> None:
        """Its files can be read without approval once it is in use."""
        if self.registry is None:
            return
        directory = skill.directory.resolve()
        if inline:
            self._inline_skill_dirs.add(directory)
        policy = self.registry.permissions
        if directory not in policy.skill_dirs:
            policy.skill_dirs += (directory,)

    def _sync_skill_dirs(self) -> None:
        if self.registry is None:
            return
        dirs: list[Path] = []
        for directory in (
            *self._inline_skill_dirs,
            *(item.skill.directory.resolve() for item in self._agent.active_skills.items()),
        ):
            if directory not in dirs:
                dirs.append(directory)
        self.registry.permissions.skill_dirs = tuple(dirs)

    def _register_skill_tools(self, skill: SkillDef) -> None:
        """The version just read replaces whatever this skill registered before."""
        self._remove_skill_tools(skill.name)
        if self.registry is None or not skill.tools:
            return
        root = self.registry.permissions.root
        for spec in skill.tools:
            self.registry.unregister(spec.full_name)
            self.registry.register(SkillTool(root, skill, spec))
        self._skill_tools[skill.name] = tuple(spec.full_name for spec in skill.tools)

    def _drop_skills(self) -> None:
        """/clear and resume: no skill is in use any more."""
        self._inline_skill_dirs.clear()
        if self.registry is not None:
            self.registry.permissions.skill_dirs = ()
        for name in list(self._skill_tools):
            self._remove_skill_tools(name)

    def _remove_skill_tools(self, name: str) -> None:
        for full_name in self._skill_tools.pop(name, ()):
            if self.registry is not None:
                self.registry.unregister(full_name)

    def _unload_skill_state(self, name: str) -> bool:
        removed_active = self._agent.active_skills.remove(name)
        had_tools = name in self._skill_tools
        self._remove_skill_tools(name)
        self._sync_skill_dirs()
        return removed_active or had_tools

    def unload_skill(self, name: str) -> bool:
        """Unload an active skill and unregister its exclusive tools while idle."""
        if self._busy:
            raise ProviderError("当前任务正在运行，不能卸载 Skill。")
        removed = self._unload_skill_state(name)
        if removed:
            self._record({"type": "skill_unload", "name": name})
        return removed

    def _unload_skill_tool(self, name: str) -> tuple[str, dict]:
        removed = self._unload_skill_state(name)
        if removed:
            self._record({"type": "skill_unload", "name": name})
            return f"已卸载 Skill {name}，规程与专属工具已移除，下一次请求起生效。", {"skill": name, "unloaded": True}
        if self.skills.get(name) is None:
            visible = [item.name for item in self.skills.skills.values() if item.model_invocable]
            close = difflib.get_close_matches(name, visible, n=3, cutoff=0.5)
            hint = f"相近的名称：{'、'.join(close)}。" if close else f"可加载：{'、'.join(visible) or '无'}。"
            raise ToolFailure("skill_unknown", f"没有名为 {name} 的 Skill。{hint}")
        return f"Skill {name} 当前未激活，无需卸载。", {"skill": name, "unloaded": False}

    def _restore_skill_snapshot(
        self,
        snapshot: tuple[tuple[str, str], ...],
        inline_tools: tuple[str, ...] = (),
    ) -> None:
        """Restore <active-skills> and dynamic skill__* tools to a checkpoint snapshot."""
        active = self._agent.active_skills
        before_skills = active.snapshot()
        before_tools = dict(self._skill_tools)
        active.clear()
        for name, arguments in snapshot:
            if self.skills.get(name) is None:
                continue
            skill, notice = self.skills.fresh(name)
            if notice:
                self._raised.append(notice)
            if not skill.model_invocable:
                continue
            try:
                active.fit_and_activate(skill, arguments, skill.render(arguments), ACTIVE_LIMIT)
            except SkillError:
                continue
        keep_tools = set(active.names) | set(inline_tools)
        for name in list(self._skill_tools):
            if name not in keep_tools:
                self._remove_skill_tools(name)
        for item in active.items():
            self._register_skill_tools(item.skill)
        self._inline_skill_dirs = {
            skill.directory.resolve() for name in inline_tools if (skill := self.skills.get(name)) is not None
        }
        self._sync_skill_dirs()
        if active.snapshot() != before_skills or self._skill_tools != before_tools:
            self._agent.meter.invalidate()
            self._agent._reset_anchor_meta()

    def prepare_skill(self, name: str, arguments: str = "") -> tuple[SkillDef, str, tuple[str, ...]]:
        """Read the skill again and check its tools; returns the skill, the request and notices.

        A fork skill's tools go to its sub-agent only; the caller sends a ForkRequest.
        """
        if self.skills.get(name) is None:
            raise ProviderError(f"未找到 Skill {name}。输入 /skills 查看。")
        skill, notice = self.skills.fresh(name)
        missing = self._missing(skill)
        if missing:
            raise ProviderError(
                f"Skill {name} 未运行：工具不可用：{'、'.join(missing)}。"
                "MCP 工具需要对应的 Server 已连接（/mcp 查看）。"
            )
        self._use_skill_dir(skill, inline=True)
        if skill.mode == "fork":
            if skill.model and self._fork_provider_config(skill.model) is None and not self._is_current(skill.model):
                names = "、".join(config.name for config in self.provider_configs) or "无"
                raise ProviderError(f"Skill {name} 未运行：没有名为 {skill.model} 的提供商（可用：{names}）。")
            return skill, skill.render(arguments, fork=True), (notice,) if notice else ()
        self._register_skill_tools(skill)
        self._record(skill_record(name, arguments, False))
        return skill, skill.render(arguments), (notice,) if notice else ()

    def _load_skill(self, name: str, arguments: str = "", *, unload: bool = False) -> tuple[str, dict]:
        """The load_skill tool; failures are tool errors and the run goes on."""
        if unload:
            return self._unload_skill_tool(name)
        visible = [item.name for item in self.skills.skills.values() if item.model_invocable]
        if self.skills.get(name) is None:
            close = difflib.get_close_matches(name, visible, n=3, cutoff=0.5)
            hint = f"相近的名称：{'、'.join(close)}。" if close else f"可加载：{'、'.join(visible) or '无'}。"
            raise ToolFailure("skill_unknown", f"没有名为 {name} 的 Skill。{hint}")
        skill, notice = self.skills.fresh(name)
        if not skill.model_invocable:
            raise ToolFailure("skill_unavailable", f"Skill {name} 只能由用户输入 /{name} 调用，不能由模型加载。")
        missing = self._missing(skill)
        if missing:
            raise ToolFailure("skill_unavailable", f"Skill {name} 需要的工具当前不可用：{'、'.join(missing)}。")
        text = skill.render(arguments)
        active = self._agent.active_skills
        refreshed = name in active
        try:
            evicted = active.fit_and_activate(skill, arguments, text, ACTIVE_LIMIT)
        except SkillError:
            raise ToolFailure(
                "skill_too_large",
                f"Skill {name} 展开后的规程超过 {ACTIVE_LIMIT} 个字符上限，未加载。"
                "请精简参数或缩短 SKILL.md 内容后再试。",
            ) from None
        for dropped in evicted:
            self._remove_skill_tools(dropped)
            self._record({"type": "skill_unload", "name": dropped})
        self._register_skill_tools(skill)
        self._sync_skill_dirs()
        self._record(skill_record(name, arguments, True))
        if notice:
            self._raised.append(notice)
        lines = [f"已{'刷新' if refreshed else '加载'} Skill {name}，规程已放入 <active-skills>，下一次请求起生效。"]
        if evicted:
            lines.append(f"已按最近最少使用顺序自动卸载：{'、'.join(evicted)}。")
        if skill.tools:
            lines.append("新增专属工具：" + "、".join(tool.full_name for tool in skill.tools))
        if skill.mode == "fork":
            lines.append(f"此 Skill 设计为独立运行，需要隔离上下文时请用户输入 /{name}。")
        if notice:
            lines.append(notice)
        details: dict[str, object] = {"skill": name, "arguments": arguments, "refreshed": refreshed}
        if evicted:
            details["evicted"] = list(evicted)
        return "\n".join(lines), details

    def use_skills(self, catalog: SkillCatalog) -> tuple[str, ...]:
        """The catalog after /skills reload; removed skills leave the active list and take their tools."""
        self.skills = catalog
        active = self._agent.active_skills
        for name in active.names:
            if catalog.get(name) is None:
                active.remove(name)
        for name in list(self._skill_tools):
            if (skill := catalog.get(name)) is None:
                self._remove_skill_tools(name)
            else:
                self._register_skill_tools(skill)
        self._sync_skill_dirs()
        return tuple(self._offer_skills())

    def _restore_skills(self, records: tuple[tuple[str, str, bool], ...]) -> list[str]:
        """Tools of every skill the session used, SOPs of the pinned ones; the rest is reported."""
        used, pinned = {}, {}
        for name, arguments, is_pinned in records:
            used[name] = None
            if is_pinned:
                pinned[name] = arguments  # The latest arguments; the first load keeps its place.
        notices, lost = [], []
        active = self._agent.active_skills
        for name in used:
            if self.skills.get(name) is None:
                lost.append(f"{name}（已不存在）")
                continue
            skill, notice = self.skills.fresh(name)
            if notice:
                notices.append(notice)
            if name in pinned:
                text = skill.render(pinned[name])
                if not skill.model_invocable:
                    lost.append(f"{name}（已不能由模型加载，规程未放回）")
                else:
                    try:
                        evicted = active.fit_and_activate(skill, pinned[name], text, ACTIVE_LIMIT)
                    except SkillError:
                        lost.append(f"{name}（规程超过 {ACTIVE_LIMIT} 个字符，未放回）")
                    else:
                        for dropped in evicted:
                            self._remove_skill_tools(dropped)
                        self._register_skill_tools(skill)
                        self._use_skill_dir(skill)
                        continue
                self._use_skill_dir(skill, inline=True)
                self._register_skill_tools(skill)
            else:
                self._use_skill_dir(skill, inline=True)
                self._register_skill_tools(skill)
        for name, _, is_pinned in records:
            if is_pinned and name in active:
                active.touch(name)
        self._sync_skill_dirs()
        if lost:
            notices.append("未恢复的 Skill：" + "、".join(lost) + "。")
        return notices

    # Fork skills ------------------------------------------------------------------------

    def _is_current(self, name: str) -> bool:
        return self.provider_info is not None and self.provider_info[0] == name

    def _fork_provider_config(self, name: str):
        return next((config for config in self.provider_configs if config.name == name), None)

    def _fork_history(self, context: str) -> list[Message]:
        """None, the last 3 turns (from a real user message, calls kept with their results), or all.

        Collapsed turns arrive as their summaries, as the main conversation sends them.
        """
        history = deepcopy(self._agent.projected())
        if context == "full":
            return history
        if context == "recent":
            starts = [i for i, m in enumerate(history) if m.role == "user" and m.synthetic is None]
            return history[starts[-3] :] if starts else []
        return []

    def _fork_registry(self, skill: SkillDef, task) -> ToolRegistry | None:
        """The main tools and its own, or only those it lists; never load_skill or another skill tool.

        Its own file tools (a copy of the main reads when it sees some history) and permission view.
        """
        if self.registry is None:
            return None
        policy = self.subagents.policy(None, task)
        root = policy.root
        own = [SkillTool(root, skill, spec) for spec in skill.tools]
        listed = set(skill.allowed_tools) - GLOBAL_BAN if skill.allowed_tools else None
        state = self.registry.file_state()
        state = state.copy() if state is not None and skill.fork_context != "none" else None
        return self.registry.for_agent(
            agent="fork",
            agent_type=task.agent_type,
            permissions=policy,
            state=state,
            keep=lambda name: name not in GLOBAL_BAN and (listed is None or name in listed),
            extra=[tool for tool in own if listed is None or tool.name in listed],
        )

    def _fork_provider(self, skill: SkillDef):
        """(provider, budget, owned); owned providers are closed after the run."""
        if not skill.model or self._is_current(skill.model):
            return self.provider, self._agent.budget, False
        config = self._fork_provider_config(skill.model)
        if config is None:
            raise ProviderError(f"Skill {skill.name} 未运行：没有名为 {skill.model} 的提供商。")
        factory = self.provider_factory
        if factory is None:
            from .providers import create_provider as factory
        try:
            return factory(config), config.budget(), True
        except Exception as exc:
            raise ProviderError(f"Skill {skill.name} 未运行：无法创建提供商 {skill.model}：{exc}") from None

    def _fork_result(self, skill: SkillDef, command: str, text: str) -> None:
        """A user message (the command) and the assistant's answer, as one exchange."""
        note = f"{command}\n\n（以上命令在独立上下文中运行了 Skill {skill.name}，结果见下一条回复。）"
        self._agent._append(Message("user", (ContentBlock("text", note),), command=command))
        self._agent._append(Message("assistant", (ContentBlock("text", text),)))

    async def _fork(self, fork: ForkRequest, command: str):
        """Run the SOP in a sub-agent with its own history; only the result joins this one."""
        main, skill = self._agent, fork.skill
        if main.busy:
            raise ProviderError("已有请求正在运行，请等待或取消。")
        provider, budget, owned = self._fork_provider(skill)
        tasks = self.subagents.tasks
        task = tasks.new(
            origin="skill", agent="fork", agent_type=f"skill:{skill.name}", description=command, prompt=fork.prompt
        )
        child = AgentLoop(
            provider,
            "",
            self._fork_registry(skill, task),
            max_iterations=main.max_iterations,
            unknown_limit=main.unknown_limit,
            policies=main.policies,
        )
        child.system, child.instructions, child.git = main.system, main.instructions, main.git  # No catalog.
        child.budget, child.auto_compact, child.session = budget, main.auto_compact, self.session
        child.context_collapse = main.context_collapse
        child.history = self._fork_history(skill.fork_context)
        self.subagents.attach(task, child)  # Usage to the task and this conversation; the task keeps a transcript.
        main.busy = True
        stopped, final, ended = None, None, False

        def end(text: str) -> None:
            nonlocal ended
            self._fork_result(skill, command, text)
            ended = True
            if task.status == "running" and not (
                stopped is not None and stopped.reason in {"completed", "max_tokens"} and final is not None
            ):
                task.status = "failed"
            tasks.settle(task, text)

        try:
            try:
                prompt = await self.subagents.start_hooks(task, fork.prompt)
                async with aclosing(child.run(prompt)) as events:
                    async for event in events:
                        if isinstance(event, RunStopped):
                            stopped = event  # The one RunStopped comes after the exchange is added.
                            continue
                        if isinstance(event, Completed):
                            final = event.answer
                        if isinstance(event, UsageUpdate):
                            event = replace(event, total=main.total_usage)  # The session total, as before.
                        elif isinstance(event, ToolProgress) and event.phase == "running":
                            task.tool_calls += 1
                        yield event
            except asyncio.CancelledError:
                task.status = "cancelled"
                end(f"[Skill {skill.name} 未完成：已取消]")
                yield RunStopped(
                    "cancelled",
                    f"已取消 Skill {skill.name}；主对话可以继续。",
                    stopped.iterations if stopped else 0,
                    stopped.results if stopped else (),
                )
                raise
            except ProviderError as exc:
                end(f"[Skill {skill.name} 未完成：{exc}]")
                raise
            if stopped is not None and stopped.reason in {"completed", "max_tokens"} and final is not None:
                end(final or "（Skill 没有输出正文。）")
            else:
                end(f"[Skill {skill.name} 未完成：{stopped.notice if stopped is not None else '没有完整回复。'}]")
            if stopped is not None:
                yield stopped
        finally:
            if not ended:  # Closed from outside, e.g. the consumer was cancelled between events.
                task.status = "cancelled"
                end(f"[Skill {skill.name} 未完成：已取消]")
            main.busy = False
            try:
                if self.commands is not None:
                    await self.commands.stop_owned(task.id)
            finally:
                if owned:
                    await provider.close()
