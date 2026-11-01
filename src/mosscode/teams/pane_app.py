"""Ch15 BE8: the window of a member in its own pane — its turns and tool calls, and its approvals.

The input box only answers approvals: a member takes its work from the lead, never from the user.
"""

from __future__ import annotations

import asyncio

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Input, OptionList, RichLog, Static

from ..appearance import ERROR_COLOR, OPERATION_COLOR, PARAMETER_COLOR
from ..messages import Completed, ProviderError, RunStopped, ToolProgress
from ..permissions import ApprovalRequest, safe_display
from .member import TYPE_LABELS

INPUT_HINT = "成员只接受 Lead 的指令，不接受新任务；需要调整它时告诉 Lead。审批出现时在这里输入序号回答。"


class MemberApp(App):
    CSS = """
    #title { height: 1; background: $primary-background; color: $text; text-style: bold; padding: 0 1; }
    #log { height: 1fr; padding: 0 1; }
    #approval { height: auto; display: none; border-top: solid $warning; padding: 0 1; }
    #approval-options { height: auto; max-height: 6; }
    #state { height: 1; padding: 0 1; color: $text-muted; }
    #input { height: 3; }
    """
    BINDINGS = [Binding("escape", "interrupt", "中断 / 拒绝"), Binding("ctrl+c", "quit", "退出", priority=True)]

    def __init__(self, process):
        super().__init__()
        self.process = process
        self.heading = f"成员 {process.name} · 团队 {process.team}"
        self._requests: list[ApprovalRequest] = []
        self._job: asyncio.Task | None = None

    def compose(self) -> ComposeResult:
        yield Static(self.heading, id="title")
        yield RichLog(id="log", wrap=True, markup=False)
        with Vertical(id="approval"):
            yield Static(id="approval-detail")
            yield OptionList(id="approval-options")
        yield Static("启动中…", id="state")
        yield Input(placeholder=INPUT_HINT, id="input")

    async def on_mount(self) -> None:
        self.title = self.heading
        self.process.on_event = self._event
        self.process.on_approval = self._approval
        self._job = asyncio.get_running_loop().create_task(self._serve())

    async def _serve(self) -> None:
        code = await self.process.serve()
        self.process.exit_code = code
        self.exit()

    async def on_unmount(self) -> None:
        if self._job is not None and not self._job.done():
            self._job.cancel()  # Its finally writes the record: stopped.
            await asyncio.gather(self._job, return_exceptions=True)

    # Events -----------------------------------------------------------------------------------------

    def _log(self, text: Text | str) -> None:
        self.query_one("#log", RichLog).write(text)

    def _state(self, text: str) -> None:
        self.query_one("#state", Static).update(text)

    def _event(self, event) -> None:
        if isinstance(event, list):  # The messages of a new turn.
            for message in event:
                line = Text(f"← {message.sender} · {TYPE_LABELS.get(message.type, message.type)}", style="bold")
                summary = message.summary or (message.body.strip().splitlines() or [""])[0]
                if summary:
                    line.append(f"：{summary[:200]}")
                self._log(line)
            self._state("运行中 · Esc 中断这一轮")
        elif isinstance(event, ToolProgress):
            if event.phase == "running":
                line = Text("● ", style=OPERATION_COLOR)
                line.append(event.call.name, style="bold")
                line.append(" " + safe_display(event.call.arguments, 160), style=PARAMETER_COLOR)
                self._log(line)
            elif event.phase == "finished" and event.result is not None:
                first = (event.result.output or "").strip().splitlines()[:1]
                style = ERROR_COLOR if event.result.is_error else "dim"
                self._log(Text("  └ " + safe_display(first[0] if first else "完成", 200), style=style))
        elif isinstance(event, Completed) and event.message.text:
            self._log(Text(event.message.text))
        elif isinstance(event, RunStopped):
            if event.reason not in {"completed", "max_tokens"}:
                self._log(Text(event.notice, style=ERROR_COLOR))
            self._state("空闲 · 等待 Lead 的消息")
        elif isinstance(event, ProviderError):
            self._log(Text(str(event), style=ERROR_COLOR))

    # Approvals --------------------------------------------------------------------------------------

    def _approval(self, request: ApprovalRequest) -> None:
        self._requests.append(request)
        if len(self._requests) == 1:
            self._show()

    def _show(self) -> None:
        from ..tui import choice_label
        panel = self.query_one("#approval")
        if not self._requests:
            panel.display = False
            self.query_one("#input", Input).focus()
            return
        request = self._requests[0]
        text = Text(f"需要授权 · {request.operation.tool}\n", style=f"bold {OPERATION_COLOR}")
        text.append(safe_display(request.operation.target), style=PARAMETER_COLOR)
        if request.operation.cwd is not None:
            text.append("\n工作目录：" + request.operation.cwd)
        text.append("\n" + request.reason)
        self.query_one("#approval-detail", Static).update(text)
        options = self.query_one("#approval-options", OptionList)
        options.clear_options()
        options.add_options([f"{index}. {choice_label(request, choice)}"
                             for index, choice in enumerate(request.choices, 1)])
        options.highlighted = 0
        panel.display = True
        options.focus()
        self._state("等待授权 · 回车确认，Esc 拒绝本次")

    def _answer(self, choice: str) -> None:
        if not self._requests:
            return
        request = self._requests.pop(0)
        self.process.conversation.registry.permissions.respond(request.id, choice)
        self._log(Text(f"  已回答：{choice}", style="dim"))
        self._state("运行中 · Esc 中断这一轮")
        self._show()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id == "approval-options" and self._requests:
            self._answer(self._requests[0].choices[event.option_index])

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        event.input.value = ""
        if self._requests and value.isdigit() and 1 <= int(value) <= len(self._requests[0].choices):
            self._answer(self._requests[0].choices[int(value) - 1])
            return
        if value:
            self._log(Text(INPUT_HINT, style=OPERATION_COLOR))

    def action_interrupt(self) -> None:
        """BE8: Esc refuses the approval on screen, otherwise ends the current turn."""
        if self._requests:
            self._answer("deny")
            return
        runner = self.process.runner
        if runner is not None and runner.interrupt():
            self._log(Text("已中断这一轮；成员会告诉 Lead。", style=OPERATION_COLOR))
