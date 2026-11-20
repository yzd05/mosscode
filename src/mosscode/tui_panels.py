"""Local command execution, shell/compact/resume flows, CommandUI, and approval/question panels."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from dataclasses import replace

from rich.text import Text
from textual.containers import VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import Input, OptionList, Static

from .appearance import MUTED_COLOR, OPERATION_COLOR, PARAMETER_COLOR, compact_notice, startup_banner
from .builtin_commands import dispatch
from .commands import CommandContext, Parsed, parse_input, unknown_note
from .config import ConfigError, ProviderConfig, refreshed
from .conversation import Notice, Resumed
from .follow import SOURCES as FOLLOWED
from .member_ui import MemberPanel
from .messages import IMAGE_MARK, CompactFailed, CompactFinished, CompactStarted, Message, ProviderError
from .permissions import Answer, ApprovalRequest, QuestionRequest, safe_display
from .runtime_factory import with_model
from .scrollback import redraw_transcript
from .tools import shell_label
from .tools.ask import OTHER as OTHER_ANSWER
from .tui_model_picker import DEFAULT_PANEL_HELP
from .tui_views import (
    CANCELLED_COMPACT,
    DECLINED_HINT,
    ChatEntry,
    CompactRow,
    PromptArea,
    ShellRow,
    _writes_only,
    choice_label,
    choice_prompt,
    preview_text,
    resumed_rows,
    rewound_image,
    shell_name,
)


class _CommandUIMixin:
    """Command dispatch, local shell/compact/resume, CommandUI, and interactive panels mixed into MossCodeApp."""

    async def _start_shell(self, command: str, *, keep: bool) -> None:
        chat = self.query_one("#chat", VerticalScroll)
        self._shell_row = ShellRow(command, keep)
        await chat.mount(self._shell_row)
        chat.anchor()
        self._generating = True
        self._tick()
        self._reply_task = asyncio.create_task(self._run_shell(command, keep))

    async def _run_shell(self, command: str, keep: bool) -> None:
        row = self._shell_row
        try:
            row.finish(await self.conversation.run_command(command, keep=keep))
        except asyncio.CancelledError:
            if not self._closing:
                row.stop("已取消 · 未加入对话")
        except ProviderError as exc:
            row.stop(f"未执行 · {exc}")
        finally:
            self._generating = False
            self._shell_row = None
            if not self._closing:
                await self._commit_ready_transcript()
                field = self.query_one("#prompt", PromptArea)
                queued = self.conversation.take_queued()
                if queued:
                    field.value = "\n\n".join((*queued, field.value) if field.value.strip() else queued)
                self._refresh_queue()
                self._refresh_context()
                self._status("")
                self.call_later(self._notify_loops_idle)

    @staticmethod
    def _note(text: str, style: str | None = None) -> Static:
        note = Static(Text(text, style=style or ""), classes="local-note")
        note.transcript = Text(text, style=style or "")
        return note

    async def _local_note(self, text: str, style: str | None = None) -> None:
        if self._closing or self._exit:  # A note queued just before exit (e.g. a task cancelled on exit) is dropped.
            return
        try:
            chat = self.query_one("#chat", VerticalScroll)
        except NoMatches:
            return
        if not chat.is_attached:
            return
        await chat.mount(self._note(text, style))
        chat.anchor()
        await self._commit_ready_transcript()

    @staticmethod
    def _compact_style(event) -> str | None:
        return OPERATION_COLOR if isinstance(event, CompactFailed) and event.code != "nothing" else None

    async def _show_compact(self, manual: bool, before=None) -> None:
        chat = self.query_one("#chat", VerticalScroll)
        self._compact_row = CompactRow(manual)
        self._compacting = self._compact_row.started
        await chat.mount(self._compact_row, before=before)
        chat.anchor()
        self._tick()

    async def _end_compact(self, text: str, style: str | None = None, before=None) -> None:
        """The running row becomes the result; without one (nothing to compact) a note is added."""
        self._compacting = None
        row, self._compact_row = self._compact_row, None
        if row is not None:
            row.finish(text, style)
            return
        chat = self.query_one("#chat", VerticalScroll)
        await chat.mount(self._note(text, style), before=before)
        chat.anchor()

    async def _start_compact(self, instructions: str) -> None:
        self.started = self._compacting = self._now()
        self._generating = True
        self._tick()
        self._reply_task = asyncio.create_task(self._run_compact(instructions))

    async def _run_compact(self, instructions: str) -> None:
        try:
            async with aclosing(self.conversation.compact(instructions)) as stream:
                async for event in stream:
                    if isinstance(event, CompactStarted):
                        await self._show_compact(event.manual)
                    elif isinstance(event, (CompactFinished, CompactFailed)):
                        await self._end_compact(compact_notice(event), self._compact_style(event))
                    elif isinstance(event, Notice):
                        await self._local_note("! " + event.text, OPERATION_COLOR)
                    await self._commit_ready_transcript()
        except asyncio.CancelledError:
            if not self._closing:
                await self._end_compact(CANCELLED_COMPACT)
        except ProviderError as exc:
            await self._end_compact(f"! {exc}", OPERATION_COLOR)
        finally:
            self._generating = False
            self._compacting = None
            if not self._closing:
                await self._commit_ready_transcript()
                field = self.query_one("#prompt", PromptArea)
                queued = self.conversation.take_queued()
                if queued:
                    field.value = "\n\n".join((*queued, field.value) if field.value.strip() else queued)
                self._refresh_queue()
                self._refresh_context()
                self._status("")
                self.call_later(self._notify_loops_idle)

    async def _clear_screen(self) -> None:
        """Like Claude Code's /clear: wipe the screen and scrollback, then start again from the banner."""
        self._recent_tools.clear()  # They belonged to the history that is gone from the screen.
        self._last_turn_entries.clear()
        if not self.is_inline:
            return
        async with self._scrollback_lock:
            self._history = [startup_banner(self.provider_name)]
            self._banner_provider = self.provider_name
            with self.batch_update():
                redraw_transcript(self, self._history)

    def _start_resume(self, session_id: str, *, startup: bool = False) -> None:
        self.started = self._now()
        self._generating = True
        self._stage("恢复会话中…")
        self._reply_task = asyncio.create_task(self._run_resume(session_id, startup))

    async def _show_resumed(self, event: Resumed) -> None:
        restored = event.restored
        self._usage = self.conversation.total_usage
        await self._clear_screen()
        when = restored.last_time.strftime("%Y-%m-%d %H:%M") if restored.last_time else "未知"
        await self._local_note(f"└ 已恢复会话 · {restored.title} · {event.messages} 条消息 · 上次活动 {when}")
        await self._show_history(restored.history)
        for notice in event.notices:
            await self._local_note("└ " + notice)

    async def _show_history(self, history) -> None:
        chat = self.query_one("#chat", VerticalScroll)
        for renderable in resumed_rows(history, shell_label(self.conversation.registry)):
            row = Static(renderable, classes="resumed-entry")
            row.transcript = renderable
            await chat.mount(row)
        chat.anchor()

    async def _run_resume(self, session_id: str, startup: bool = False) -> None:
        unanswered: tuple[str, ...] = ()
        try:
            if startup:
                await self._confirm_hooks()
            async with aclosing(self.conversation.resume(session_id)) as stream:
                async for event in stream:
                    if isinstance(event, Resumed):
                        unanswered = event.restored.unanswered
                        await self._show_resumed(event)
                    elif isinstance(event, CompactStarted):
                        await self._show_compact(event.manual)
                    elif isinstance(event, (CompactFinished, CompactFailed)):
                        await self._end_compact(compact_notice(event), self._compact_style(event))
                    elif isinstance(event, Notice):
                        await self._local_note("! " + event.text, OPERATION_COLOR)
                    await self._commit_ready_transcript()
        except asyncio.CancelledError:
            if not self._closing and self._compact_row is not None:
                await self._end_compact(CANCELLED_COMPACT)
        except ProviderError as exc:
            if not self._closing and self._compact_row is not None:
                await self._end_compact(f"! {exc}", OPERATION_COLOR)
            else:
                await self._local_note(f"! {exc}", OPERATION_COLOR)
        finally:
            self._generating = False
            self._compacting = None
            if not self._closing:
                await self._commit_ready_transcript()
                field = self.query_one("#prompt", PromptArea)
                # Unanswered messages come back for the user to edit or resend.
                texts = [*unanswered, *self.conversation.take_queued()]
                if texts:
                    field.value = "\n\n".join((*texts, field.value) if field.value.strip() else texts)
                self._refresh_queue()
                self._refresh_context()
                self._status("")
                self.call_later(self._notify_loops_idle)
                if startup:
                    self._resume = None
                    self._startup_task = asyncio.create_task(self._initialize())

    def _show_panel(self, title: str, rows: int) -> None:
        self._panel_rows = rows
        self.query_one("#model-title", Static).update(title)
        options = self.query_one("#models", OptionList)
        # One row per choice, so the panel height reserved in _fit_chat holds.
        options.styles.height = min(8, rows)
        self.query_one("#model-panel").display = True
        self._place_prompt()
        options.focus()
        self._show_members()  # Hidden while the model panel is open.
        self.call_later(self._fit_chat)

    async def _command(self, typed: str, parsed: Parsed) -> None:
        """Echo what was typed, then run it; prompt commands echo the command, not the template."""
        chat = self.query_one("#chat", VerticalScroll)
        await chat.mount(ChatEntry("You", typed))
        chat.anchor()
        await self._commit_ready_transcript()
        if parsed.kind == "unknown":
            await self._local_note(unknown_note(parsed, self.commands))
            return
        try:
            await dispatch(self._command_context(), parsed)
        except ProviderError as exc:
            await self._local_note(f"! {exc}", OPERATION_COLOR)

    def _command_context(self) -> CommandContext:
        return CommandContext(self.conversation, self, self.commands, self.settings)

    # CommandUI -------------------------------------------------------

    async def note(self, text: str, style: str | None = None) -> None:
        await self._local_note(text, style)

    async def submit(
        self,
        prompt: str,
        *,
        command: str | None = None,
        fork=None,
        synthetic: str | None = None,
        images: dict | None = None,
    ) -> None:
        if synthetic is not None and self._task_running:
            return  # A resumed goal continues when the running turn ends.
        await self._start_request(
            prompt, show_user=False, command=command, fork=fork, images=images, synthetic=synthetic
        )

    def refresh_status(self) -> None:
        self._usage = self.conversation.total_usage
        self._refresh_context()
        self._status("")
        self.call_later(self._notify_loops_idle)

    async def clear_screen(self) -> None:
        await self._clear_screen()

    def choose(self, title: str, options, current: int, on_pick, on_cancel=None) -> None:
        self._model_picker_active = False
        self._model_picker_choices = []
        self._model_picker_filtered = []
        if self.query("#model-filter"):
            search = self.query_one("#model-filter", Input)
            search.display = False
            search.value = ""
        self.query_one("#model-help", Static).update(DEFAULT_PANEL_HELP)
        picker = self.query_one("#models", OptionList)
        picker.clear_options()
        picker.add_options(list(options))
        self._pick, self._on_cancel = on_pick, on_cancel
        self._show_panel(title, len(options))
        picker.highlighted = current

    async def compact(self, instructions: str) -> None:
        await self._start_compact(instructions)

    def resume(self, session_id: str) -> None:
        self._start_resume(session_id)

    async def rewound(self, message: Message) -> None:
        await self._clear_screen()
        await self._show_history(self.conversation.history)
        images = {}
        for block in message.content:
            if block.kind == "image" and block.image is not None and IMAGE_MARK.fullmatch(block.image.alt):
                try:
                    images[block.image.alt] = rewound_image(block.image)
                except OSError:
                    continue  # Its file is gone: the placeholder stays as text.
        if message.command is not None:
            images = {}
        self.query_one("#prompt", PromptArea).restore_message(message.shown, images)
        self._refresh_context()

    def _open_rewind(self) -> None:
        self.run_worker(dispatch(self._command_context(), parse_input("/rewind", self.commands)), group="rewind")

    async def switch_model(self, name: str, model: str | None = None) -> bool:
        if self.settings is None:
            await self._local_note(f"切换失败：未知 Provider：{name}。")
            return False
        config = next((item for item in self.settings.providers if item.name == name), None)
        if config is None:
            await self._local_note(f"切换失败：未知 Provider：{name}。")
            return False
        configured = next((item for item in self._configured_providers if item.name == name), config)
        if model is not None:
            base = configured
            try:
                if base.follow:
                    base = refreshed(base)
                    from .credentials import remember

                    remember(base.api_key)
                config = with_model(base, model)
            except ConfigError as exc:
                await self._local_note(f"切换失败：{exc}")
                return False
            current_model = self.model or (
                self.conversation.provider_config.model if self.conversation.provider_config is not None else ""
            )
            if config.name == self.provider_name and config.model == current_model:
                await self._local_note(f"└ 已在使用 {config.name}（{config.model}）。")
                return True
            if await self._switch_model(config, force=True):
                providers = tuple(config if item.name == name else item for item in self.settings.providers)
                self.settings = replace(self.settings, providers=providers)
                self.conversation.provider_configs = providers
                return True
            return False
        if not configured.follow:
            same_model = not self.model or configured.model == self.model
            if configured.name == self.provider_name and same_model and configured is config:
                await self._local_note(f"└ 已在使用 {configured.name}（{configured.model}）。")
                return True
            if await self._switch_model(configured, force=configured is not config):
                if configured is not config:
                    # An earlier model override replaced this provider: switching by name restores its configured model.
                    providers = tuple(configured if item.name == name else item for item in self.settings.providers)
                    self.settings = replace(self.settings, providers=providers)
                    self.conversation.provider_configs = providers
                return True
            return False
        # Read again: the tool it follows may have been switched (in CC Switch, say) since MossCode started.
        try:
            config = refreshed(configured)
        except ConfigError as exc:
            await self._local_note(f"切换失败：{exc}")
            return False
        from .credentials import remember

        remember(config.api_key)  # A key switched to in that tool since startup is kept out of output too.
        if await self._switch_model(config, again=config.name == self.provider_name):
            providers = tuple(config if item.name == name else item for item in self.settings.providers)
            self.settings = replace(self.settings, providers=providers)
            self.conversation.provider_configs = providers  # Skills and sub-agents that name it get this one.
            self._configured_providers = tuple(
                config if item.name == name else item for item in self._configured_providers
            )
            return True
        return False

    async def _switch_model(self, config: ProviderConfig, *, again: bool = False, force: bool = False) -> bool:
        """again: the current provider, read again from the tool it follows."""
        same_model = not self.model or config.model == self.model
        if config.name == self.provider_name and same_model and not again and not force:
            await self._local_note(f"└ 已在使用 {config.name}（{config.model}）。")
            return False
        try:
            provider = self.provider_factory(config)
            await self.conversation.set_provider(
                provider, config.budget(), (config.name, config.protocol, config.model)
            )
        except (ProviderError, ConfigError) as exc:
            await self._local_note(f"切换失败：{exc}")
            return False
        self.provider_name, self.model = config.name, config.model
        self._record_recent_model(config.name, config.model)
        if self.query("#banner"):  # Not yet committed to the scrollback: it can still change.
            self.query_one("#banner", Static).update(startup_banner(self.provider_name))
            self._banner_provider = self.provider_name
        self._refresh_context()
        self._status("")
        if again:
            await self._local_note(
                f"└ 已重新读取 {FOLLOWED[config.follow]} 的设置：{config.name}"
                f"（{config.protocol} / {config.model}）；对话历史保留。"
            )
        else:
            await self._local_note(f"└ 已切换到 {config.name}（{config.protocol} / {config.model}）；对话历史保留。")
        if note := config.window_note():
            await self._local_note(f"└ {note}")
        if (problem := self.conversation.thinking_problem()) is not None:
            await self._local_note(
                f"└ 思考强度 {self.conversation.thinking}：{config.name} 不支持（{problem}），这个模型的请求按配置发送。"
            )
        return True

    def _close_model_panel(self) -> None:
        self._pick = self._on_cancel = None
        self._model_picker_active = False
        self._model_picker_choices = []
        self._model_picker_filtered = []
        if self.query("#model-filter"):
            search = self.query_one("#model-filter", Input)
            search.display = False
            search.value = ""
        self.query_one("#model-panel").display = False
        self._place_prompt()
        if not self._approval_request and not self._question:
            self._focus_home()
        self._show_members()
        self.call_later(self._fit_chat)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "model-filter" and getattr(self, "_model_picker_active", False):
            self._render_model_picker(event.value)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "model-filter" and getattr(self, "_model_picker_active", False):
            choice = self._current_picker_choice()
            if choice is not None:
                await self._apply_model_choice(choice)
        elif event.input.id == "connect-input":
            await self._handle_connect_input(event.value)

    async def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id == "models":
            if getattr(self, "_model_picker_active", False):
                choice = self._current_picker_choice()
                if choice is not None:
                    await self._apply_model_choice(choice)
                return
            pick = self._pick
            self._close_model_panel()
            if pick is not None:
                await pick(event.option_index)
        elif event.option_list.id == "connect-options":
            await self._handle_connect_option(event.option_index)
        elif event.option_list.id == "approval-options" and self._approval_request:
            self._answer_approval(self._approval_request.choices[event.option_index])
        elif event.option_list.id == "question-options" and self._question is not None:
            self._pick_question(event.option_index)
        elif event.option_list.id == "member-list":  # Enter opens the member view.
            rows = self.query_one("#member-panel", MemberPanel).rows
            if event.option_index < len(rows):
                await self._open_view(rows[event.option_index].name)

    def _waiting(self, request: ApprovalRequest) -> bool:
        registry = self.conversation.registry
        future = registry.permissions.pending.get(request.id) if registry is not None else None
        return future is not None and not future.done()

    def _member_approval(self, request: ApprovalRequest) -> None:
        """A member's request; once the window is closing there is no card to show, so it is refused."""
        if self._closing or not self.query("#approval-panel"):
            teams = getattr(self.conversation, "teams", None)
            if teams is not None and request.id in teams.forwarded:
                return  # A pane member still asks in its own window.
            if self.conversation.registry is not None:
                self.conversation.registry.permissions.respond(request.id, "deny")
            return
        self._show_approval(request)

    def _show_approval(self, request: ApprovalRequest) -> None:
        if self._question is not None:
            if all(item.id != request.id for item in self._approval_queue):
                self._approval_queue.append(request)  # After the questions.
            return
        if self._approval_request is not None and self._approval_request.id != request.id:
            if all(item.id != request.id for item in self._approval_queue):
                self._approval_queue.append(request)  # After the one on screen.
                self._status(self._status_note)
            return
        self._approval_request = request
        operation = request.operation
        labels = {
            "read_file": "读取文件",
            "write_file": "写入文件",
            "edit_file": "修改文件",
            "glob": "查找文件",
            "grep": "搜索内容",
            "repo_map": "生成代码地图",
            "exit_plan_mode": "审阅计划",
            "execute_command": shell_name(shell_label(self.conversation.registry)),
        }
        text = Text()
        if operation.tool == "team_merge":
            text.append(f"需要确认 · 合并成员 {safe_display(operation.target)} 的分支", style=f"bold {OPERATION_COLOR}")
        else:
            text.append(
                "需要授权 · "
                + ("沙箱中的命令联网" if operation.network else labels.get(operation.tool, operation.tool))
                + (" · 在沙箱外运行" if operation.unsandboxed else "")
                + (" · 沙箱只限制写入" if _writes_only(operation) else "")
                + (f" · {request.source}" if request.source else "")
                + "\n",
                style=f"bold {OPERATION_COLOR}",
            )
            text.append(safe_display(operation.target), style=PARAMETER_COLOR)
        if operation.cwd is not None:
            text.append("\n工作目录：" + operation.cwd)
        text.append("\n" + request.reason, style=MUTED_COLOR)
        if operation.tool == "exit_plan_mode":
            text.append("\n完整计划显示在上方对话中。")
        elif operation.preview:
            text.append("\n")
            text.append_text(preview_text(operation.preview))
        self._approval_text = text
        self.query_one("#approval-detail", Static).update(text)
        self.query_one("#approval-panel").display = True
        self._place_prompt()
        options = self.query_one("#approval-options", OptionList)
        options.clear_options()
        options.add_options([choice_prompt(choice_label(request, choice)) for choice in request.choices])
        options.styles.height = len(request.choices)
        options.highlighted = 0
        members = self.query_one("#member-panel", MemberPanel)
        if members.expanded:  # Compact while a card waits.
            self._stop_armed = None
            members.collapse(self._now())
        options.focus()
        self._show_members()
        if not self._generating:  # A member's request while the lead waits for input.
            self._status("等待授权 · Esc 拒绝本次")
        else:
            self._status(self._status_note)  # The member approval count.
        self._tick()
        self.call_later(self._fit_chat)

    def _show_question(self, request: QuestionRequest) -> None:
        if self._question is not None or self._closing or not self.query("#question-panel"):
            return
        if self._approval_request is not None:  # A member's card; the questions come next.
            self._question_waiting = request
            return
        self._question, self._question_step, self._question_answers = request, 0, []
        self._question_checked, self._question_typing = set(), False
        members = self.query_one("#member-panel", MemberPanel)
        if members.expanded:
            self._stop_armed = None
            members.collapse(self._now())
        self._render_question()

    def _question_labels(self) -> list[Text]:
        question = self._question.questions[self._question_step]
        labels = []
        for index, (label, description) in enumerate(question.options):
            if question.multi_select:
                mark = "[x] " if index in self._question_checked else "[ ] "
            else:
                mark = f"{index + 1}. "
            text = Text(mark + safe_display(label, 30))
            if description:
                text.append(" — " + safe_display(description, 200), style=MUTED_COLOR)
            labels.append(text)
        labels.append(Text(("    " if question.multi_select else f"{len(question.options) + 1}. ") + OTHER_ANSWER))
        return labels

    def _render_question(self, highlighted: int = 0) -> None:
        request = self._question
        question = request.questions[self._question_step]
        text = Text(question.header, style=f"bold {OPERATION_COLOR}")
        if len(request.questions) > 1:
            text.append(f" · 第 {self._question_step + 1}/{len(request.questions)} 题", style=f"bold {OPERATION_COLOR}")
        if question.multi_select:
            text.append(" · 可多选", style=f"bold {OPERATION_COLOR}")
        if request.source:
            text.append(f" · {request.source}")
        text.append("\n" + safe_display(question.question, 1000))
        self.query_one("#question-detail", Static).update(text)
        options = self.query_one("#question-options", OptionList)
        options.clear_options()
        options.add_options(self._question_labels())
        options.highlighted = highlighted
        self.query_one("#question-help", Static).update(
            "↑ / ↓ 选择 · 空格勾选 · Enter 确认 · Esc 不回答"
            if question.multi_select
            else "↑ / ↓ 选择 · Enter 确认 · Esc 不回答 · Ctrl+C 取消任务"
        )
        self.query_one("#question-panel").display = True
        self._place_prompt()
        options.focus()
        self._show_members()
        self._stage("等待回答 · Esc 不回答")
        self.call_later(self._fit_chat)

    def _toggle_question(self) -> None:
        if self._question is None or self._question_typing:
            return
        question = self._question.questions[self._question_step]
        index = self.query_one("#question-options", OptionList).highlighted
        if not question.multi_select or index is None or index >= len(question.options):
            return
        self._question_checked ^= {index}
        self._render_question(index)

    def _pick_question(self, index: int) -> None:
        question = self._question.questions[self._question_step]
        if index >= len(question.options):  # "其他": the prompt takes the answer.
            self._question_typing = True
            self._place_prompt()
            field = self.query_one("#prompt", PromptArea)
            self._question_draft, field.value = field.value, ""
            self.query_one("#question-help", Static).update("在输入框中输入你的回答 · Enter 提交 · Esc 返回选项")
            field.focus()
            self._stage("等待回答 · 输入回答后 Enter 提交 · Esc 返回选项")
            return
        picked = sorted(self._question_checked) if question.multi_select and self._question_checked else [index]
        self._next_question(Answer(tuple(question.options[i][0] for i in picked)))

    def _next_question(self, answer: Answer) -> None:
        if self._question_typing:
            self._question_typing = False
            self.query_one("#prompt", PromptArea).value = self._question_draft
        self._question_answers.append(answer)
        self._question_checked = set()
        if len(self._question_answers) < len(self._question.questions):
            self._question_step += 1
            self._render_question()
            return
        self._finish_question()

    def _finish_question(self) -> None:
        """Send the answers given so far; the remaining questions go unanswered."""
        request = self._question
        if request is None:
            return
        answers = tuple(self._question_answers)
        answers += (None,) * (len(request.questions) - len(answers))
        if self.conversation.registry is not None:
            self.conversation.registry.permissions.answer(request.id, answers)
        self._hide_question()

    def _hide_question(self) -> None:
        if self._question is None:
            return
        if self._question_typing and not self._closing:
            self.query_one("#prompt", PromptArea).value = self._question_draft
        self._question, self._question_answers, self._question_checked = None, [], set()
        self._question_step, self._question_typing, self._question_draft = 0, False, ""
        if self._closing:
            return
        self.query_one("#question-panel").display = False
        self._place_prompt()
        if self._approval_queue:
            self._hide_approval()  # A member's card that waited comes up.
        else:
            self._focus_home()
            self._show_members()
        self._status(self._status_note if self._generating else "")
        self._tick()
        self.call_later(self._fit_chat)
        self.call_later(self._notify_loops_idle)

    def _hide_approval(self) -> None:
        """The next request still waiting stays or comes up (a member's outlives the lead's turn)."""
        waiting = [
            item
            for item in ([self._approval_request] if self._approval_request else []) + self._approval_queue
            if self._waiting(item)
        ]
        self._approval_request = None
        self._approval_queue = waiting[1:]
        if waiting and not self._closing:
            self._show_approval(waiting[0])
            return
        self.query_one("#approval-panel").display = False
        self._place_prompt()
        question, self._question_waiting = self._question_waiting, None
        if question is not None and self._waiting(question) and not self._closing:
            self._show_question(question)
            return
        if not self._closing:
            # The prompt stays usable while the task runs, e.g. to queue a correction.
            self._focus_home()
            self._show_members()
            self.call_later(self._notify_loops_idle)
        self.call_later(self._fit_chat)

    def _withdraw_approval(self, request_id: str) -> None:
        """A pane member's or background sub-agent's request answered elsewhere or cancelled."""
        if self._closing:
            return
        self._approval_queue = [item for item in self._approval_queue if item.id != request_id]
        if not self.query("#approval-panel"):
            if self._approval_request is not None and self._approval_request.id == request_id:
                self._approval_request = None
            return
        if self._approval_request is not None and self._approval_request.id == request_id:
            self._approval_request = None
            self._hide_approval()  # The next one still waiting comes up.
            if self._approval_request is None and not self._generating:
                self._status("")
        else:
            self._status(self._status_note)
            self._show_members()

    def _answer_approval(self, choice: str) -> None:
        if self._approval_request and self.conversation.registry:
            request, self._approval_request = self._approval_request, None
            self.conversation.registry.permissions.respond(request.id, choice)
            self._hide_approval()
            if request.operation.tool == "team_merge" and choice == "deny":
                self.call_later(self._local_note, DECLINED_HINT)  # The reason is the user's next message.
            if self._approval_request is None and not self._generating:
                self._status("")
            self._tick()
