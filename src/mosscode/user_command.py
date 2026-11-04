"""! commands: what the user runs from the prompt, in the shell execute_command uses, without approval.

The command and its output can join the history as a program-written message, so the model sees them with the
next request; running one never starts a reply.
"""

import re
from dataclasses import dataclass
from time import monotonic

from pydantic import ValidationError

from .messages import SHELL, ContentBlock, Message
from .tools.base import ToolFailure
from .tools.process import run_process

OUTPUT_HEAD = OUTPUT_TAIL = 16 * 1024  # Bytes kept at each end: failure summaries are usually at the end.
COMMAND = re.compile(r"<command>\n(.*?)\n</command>", re.DOTALL)


@dataclass(frozen=True)
class CommandRun:
    command: str
    output: str  # stdout, then stderr under its own heading.
    exit_code: int | None  # None when the command did not run.
    problem: str = ""  # Why it did not run.
    elapsed: float = 0.0
    truncated: bool = False
    shell: str = ""


async def run_user_command(command: str, registry) -> CommandRun:
    """Runs command in the project root; only the hard checks and deny rules can stop it."""
    tool = registry.get("execute_command")
    if tool is None:
        return CommandRun(command, "", None, "当前没有启用 execute_command。")
    try:
        params = tool.validate_arguments({"command": command})
    except (ToolFailure, ValidationError):
        return CommandRun(command, "", None, "命令不合法。")
    refused = registry.refusal(tool, params)
    if refused is not None:
        return CommandRun(command, "", None, refused.reason, shell=tool.shell.label)
    started = monotonic()
    try:
        details = await run_process(
            tool.shell.args(command),
            tool.root,
            check=False,
            limit=OUTPUT_HEAD,
            tail=OUTPUT_TAIL,
            env=tool.shell.env(command),
        )
    except ToolFailure as exc:
        return CommandRun(command, "", None, str(exc), shell=tool.shell.label)
    except OSError as exc:
        return CommandRun(
            command, "", None, f"无法启动 {tool.shell.label}：{exc.strerror or exc}", shell=tool.shell.label
        )
    output = details["stdout"]
    if details["stderr"]:
        output += ("\n" if output and not output.endswith("\n") else "") + "stderr:\n" + details["stderr"]
    return CommandRun(
        command,
        output.rstrip("\n"),
        details["exit_code"],
        elapsed=monotonic() - started,
        truncated=details["truncated"],
        shell=tool.shell.label,
    )


def command_message(run: CommandRun) -> Message:
    """The command and its output for the model, marked as the user's own."""
    text = (
        f"用户在输入框中用 ! 直接执行了下面的命令（不是你调用的；shell：{run.shell}，目录：项目根目录），"
        f"退出码 {run.exit_code}。这条消息只是让你了解结果，用户的下一条消息才是要你做的事。\n"
        f"<command>\n{run.command}\n</command>\n<output>\n{run.output or '（无输出）'}\n</output>"
    )
    return Message("user", (ContentBlock("text", text),), synthetic=SHELL)


def ran(message: Message) -> str:
    """The command a shell-command message holds, for showing it again after a resume."""
    match = COMMAND.search(message.text)
    return match.group(1) if match else ""
