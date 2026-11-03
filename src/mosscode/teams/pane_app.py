"""The window of a member in its own pane — its turns and tool calls, and its approvals.

The input box only answers approvals: a member takes its work from the lead, never from the user.
"""

from __future__ import annotations

import asyncio

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Input, OptionList, RichLog, Static

from ..appearance import OPERATION_COLOR, PARAMETER_COLOR
from ..messages import RunStopped
from ..permissions import ApprovalRequest, safe_display
from .view import Feed

INPUT_HINT = "成员只接受 Lead 的指令，不接受新任务；需要调整它时告诉 Lead。审批出现时在这里输入序号回答。"


class MemberApp(App):
    CSS = """
    #title { height: 1; background: $primary-background; color: $text; text-style: bold; padding: 0 1; }
    #log { height: 1fr; padding: 0 1; }
    #running { height: auto; max-height: 6; display: none; padding: 0 1; }
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
        self._feed = Feed()

    def compose(self) -> ComposeResult:
        yield Static(self.heading, id="title")
        yield RichLog(id="log", wrap=True, markup=False)
        yield Static("", id="running", markup=False)
        with Vertical(id="approval"):
            yield Static(id="approval-detail")
            yield OptionList(id="approval-options")
        yield Static("启动中…", id="state")
        yield Input(placeholder=INPUT_HINT, id="input")

    async def on_mount(self) -> None:
        self.title = self.heading
        self.process.on_event = self._event
        self.process.on_approval = self._approval
        self.process.on_settled = self._settled
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
        for line in self._feed.event(event):
            self._log(line)
        running = self.query_one("#running", Static)
        running.update(self._feed.running_text())
        running.display = bool(self._feed.waiting)
        if isinstance(event, list):  # The messages of a new turn.
            self._state("运行中 · Esc 中断这一轮")
        elif isinstance(event, RunStopped):
            self._state("空闲 · 等待 Lead 的消息")

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
        options.add_options(
            [f"{index}. {choice_label(request, choice)}" for index, choice in enumerate(request.choices, 1)]
        )
        options.highlighted = 0
        panel.display = True
        options.focus()
        self._state("等待授权 · 回车确认，Esc 拒绝本次")

    def _answer(self, choice: str) -> None:
        if not self._requests:
            return
        request = self._requests.pop(0)
        self.process.conversation.registry.permissions.respond(request.id, choice)
        self.process.answered_here()
        self._log(Text(f"  已回答：{choice}", style="dim"))
        self._state("运行中 · Esc 中断这一轮")
        self._show()

    def _settled(self, request_id: str, choice: str | None) -> None:
        """Answered in the lead's window (choice), or no longer waiting: the card goes."""
        from ..tui import choice_label

        request = next((item for item in self._requests if item.id == request_id), None)
        if request is None:
            return
        shown = self._requests[0] is request
        self._requests.remove(request)
        if choice is not None:
            self._log(Text(f"  已在 Lead 的窗口中回答：{choice_label(request, choice)}", style="dim"))
        if shown:
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
        """Esc refuses the approval on screen, otherwise ends the current turn."""
        if self._requests:
            self._answer("deny")
            return
        runner = self.process.runner
        if runner is not None and runner.interrupt():
            self._log(Text("已中断这一轮；成员会告诉 Lead。", style=OPERATION_COLOR))
