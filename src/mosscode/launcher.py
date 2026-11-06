"""The `moss` / `mosscode` command.

Arguments, --help, --version and the subcommands are handled with nothing heavy imported; a session
imports the rest through cli.run.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

OUTPUT_FORMATS = ("text", "json", "stream-json")
SUBCOMMANDS = ("setup", "doctor", "models", "key", "plugin", "sandbox")
NAMES = ("moss", "mosscode")
USAGE = "%(prog)s [选项] [PROMPT]\n       %(prog)s setup | doctor | models | key | plugin | sandbox"
DESCRIPTION = "MossCode：终端中的 coding agent。在项目目录中运行即可开始。"
EXAMPLES = """\
子命令：
  setup                 创建用户级配置 ~/.mosscode/config.yaml
  doctor                检查安装、配置和依赖工具，不发送请求
  models [关键词|update] 查找模型目录中的窗口和价格，或下载最新目录
  key set|delete 名称    在系统凭据管理器中保存或删除 API 密钥
  plugin install|list|update|remove|enable|disable
                        安装和管理插件（Skill、命令、子 Agent、MCP、Hook）
  sandbox cleanup [项目] 移除 Windows 受限令牌沙箱加在目录上的访问控制

例子：
  moss                                   在当前目录启动交互界面
  moss "解释这个项目的入口"               启动后立即发送这条消息
  moss -p "列出 src 下的模块"             无界面执行一次任务后退出
  Get-Content error.log | moss -p "解释这个错误"
  moss -c                                恢复本项目最近的会话

moss 与 mosscode 是同一个命令。要把 setup、doctor、models、key、plugin 或 sandbox 当作消息发送，写成 moss -- doctor。
"""


def version() -> str:
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as installed

    try:
        return installed("mosscode")
    except PackageNotFoundError:  # Run from a source tree that was never installed.
        return "0+unknown"


def program() -> str:
    name = Path(sys.argv[0]).stem if sys.argv and sys.argv[0] else ""
    return name if name in NAMES else "moss"


def _permission_mode(value: str) -> str:
    from .permissions import PermissionConfigError, normalize_mode

    try:
        return normalize_mode(value)
    except PermissionConfigError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=program(),
        usage=USAGE,
        description=DESCRIPTION,
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "initial", nargs="?", metavar="PROMPT", help="启动后立即发送的第一条消息；与 -p 一起用时是无界面执行的任务"
    )
    parser.add_argument("-v", "--version", action="version", version=f"mosscode {version()}", help="显示版本后退出")
    parser.add_argument(
        "-p", "--prompt", nargs="?", const="", help="无界面执行一次任务并输出结果；管道输入附在任务之后"
    )
    parser.add_argument(
        "-c", "--continue", dest="continue_session", action="store_true", help="恢复本项目最近活动的会话"
    )
    parser.add_argument(
        "--resume", nargs="?", const="", metavar="ID", help="恢复指定会话；不带 ID 时在交互界面中打开会话列表"
    )
    parser.add_argument("--config", help="YAML 配置路径；默认依次查找 .mosscode/config.yaml、~/.mosscode/config.yaml")
    parser.add_argument("--provider", help="指定命名 Provider，跳过启动选择；无界面模式默认使用 default_provider")
    parser.add_argument(
        "--permission-mode",
        type=_permission_mode,
        metavar="MODE",
        help="权限档位：default/acceptEdits/plan/bypassPermissions；仍遵守硬限制和显式拒绝",
    )
    parser.add_argument(
        "--output-format",
        choices=OUTPUT_FORMATS,
        default="text",
        help="无界面输出格式：text（默认，流式正文）、json（结束时输出一个结果对象）、stream-json（每行一个 JSON 事件）",
    )
    parser.add_argument("--show-usage", action="store_true", help="text 格式在 stderr 显示逐次请求的 Token 与缓存用量")
    parser.add_argument("--list-providers", action="store_true", help="列出配置，不发请求")
    parser.add_argument("--no-hooks", action="store_true", help="本次运行不加载任何 Hook")
    parser.add_argument(
        "--sandbox",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="本次运行打开（--sandbox）或关闭（--no-sandbox）命令沙箱，覆盖配置中的 sandbox.mode",
    )
    parser.add_argument(
        "--trust-project-hooks",
        action="store_true",
        help="本次运行信任项目级 Hook 文件（不写入确认记录），用于无界面模式",
    )
    parser.add_argument(
        "--trust-project-plugins",
        action="store_true",
        help="本次运行信任项目声明的插件（不写入确认记录），用于无界面模式",
    )
    # A team member in a pane of its own; started by the lead, not by hand.
    parser.add_argument("--team-dir", help=argparse.SUPPRESS)
    parser.add_argument("--team-member", help=argparse.SUPPRESS)
    return parser


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse and check the combinations; exits with 2 on a mistake, as argparse does."""
    command = parser()
    args = command.parse_args(argv)
    if (args.team_dir is None) != (args.team_member is None):
        command.error("--team-dir 和 --team-member 需要一起使用。")
    if args.prompt is not None:  # The task is the value of -p or the positional prompt, not both.
        if args.prompt and args.initial is not None:
            command.error("无界面任务只能给一个：-p 的值或位置参数 PROMPT。")
        if not args.prompt and args.initial is not None:
            args.prompt = args.initial
        args.initial = None
    if args.output_format != "text" and args.prompt is None:
        command.error("--output-format 只用于 -p/--prompt 无界面模式。")
    if args.continue_session and args.resume is not None:
        command.error("--continue 和 --resume 只能选择一个。")
    if args.resume == "" and args.prompt is not None:
        command.error("无界面模式中 --resume 需要会话 ID；恢复最近的会话请用 --continue。")
    if args.resume == "" and args.initial is not None:
        command.error("打开会话列表时不能同时给出第一条消息；请用 --resume ID 或 -c。")
    return args


def is_terminal(stream) -> bool:
    try:
        return stream is not None and stream.isatty()
    except (AttributeError, ValueError, OSError):
        return False


def with_stdin(prompt: str, stdin=None) -> str:
    """Piped input follows the task after a blank line; alone, it is the task."""
    stdin = sys.stdin if stdin is None else stdin
    if is_terminal(stdin) or stdin is None:
        return prompt
    try:
        piped = stdin.read()
    except (OSError, ValueError, UnicodeError):
        return prompt
    piped = piped.strip("\n")
    if not piped.strip():
        return prompt
    return f"{prompt}\n\n{piped}" if prompt.strip() else piped


def _subcommand(name: str, argv: list[str]) -> int:
    if name == "setup":
        from .setup import main as setup

        return setup(argv)
    if name == "models":
        from .models_command import main as models

        return models(argv)
    if name == "key":
        from .key_command import main as key

        return key(argv)
    if name == "plugin":
        from .plugin_command import main as plugin

        return plugin(argv)
    if name == "sandbox":
        from .sandbox_command import main as sandbox

        return sandbox(argv)
    from .doctor import main as doctor

    return doctor(argv)


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] in SUBCOMMANDS:
        raise SystemExit(_subcommand(argv[0], argv[1:]))
    args = parse_arguments(argv)
    if args.team_dir is None and not args.list_providers:
        if args.prompt is not None:
            args.prompt = with_stdin(args.prompt)
            if not args.prompt.strip():
                parser().error("-p 需要任务：写在 -p 之后，或通过管道输入。")
        elif not is_terminal(sys.stdin):
            print("MossCode: 交互界面需要终端；无界面执行请用 -p。", file=sys.stderr)
            raise SystemExit(2)
    from .cli import run

    run(args)
