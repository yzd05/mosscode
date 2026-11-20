"""`moss config`: inspect active configuration or launch the local tokenized Web configuration UI (`--web`)."""

from __future__ import annotations

import argparse
import math
import sys
import threading
import webbrowser
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any


def main(
    argv: list[str] | None = None,
    *,
    home: Path | None = None,
    write: Callable[[str], None] = print,
    err_write: Callable[[str], None] | None = None,
    open_browser: Callable[[str], Any] | None = webbrowser.open,
) -> int:
    err = err_write or (lambda msg: print(msg, file=sys.stderr))
    command = argparse.ArgumentParser(
        prog="moss config",
        description="查看当前模型配置概要，或通过 `--web` 启动仅限本机访问的临时 Web 配置面板。",
    )
    command.add_argument("--web", action="store_true", help="启动绑定在 127.0.0.1 的临时带令牌 Web 配置页")
    command.add_argument("--port", type=int, default=0, help="与 --web 配合使用，指定监听端口（默认 0 自动分配）")
    command.add_argument("--no-browser", action="store_true", help="启动 --web 时不自动打开浏览器")
    command.add_argument(
        "--idle-timeout",
        type=float,
        default=1800.0,
        help="无操作自动退出的秒数（默认 1800；设 0 表示不自动超时）",
    )
    command.add_argument("--config", help="查看指定配置文件路径（不带 --web 时生效）")
    args = command.parse_args(argv)

    if args.port < 0 or args.port > 65535:
        err("moss config：--port 必须是 0–65535 之间的整数。")
        return 2
    if not math.isfinite(args.idle_timeout) or args.idle_timeout < 0:
        err("moss config：--idle-timeout 必须是非负有限秒数。")
        return 2

    if not args.web:
        from .login_command import _status_report

        rc = _status_report(home, Path(args.config) if args.config else None, write)
        write("提示：运行 `moss config --web` 打开浏览器可视化配置页，或运行 `moss login` 进行 OAuth 登录。")
        return rc

    from .config_web import ConfigWebServer

    try:
        with ConfigWebServer(home=home, port=args.port, idle_timeout=args.idle_timeout) as server:
            write("MossCode 本地配置中心已启动（仅监听 127.0.0.1，请勿分享此含一次性令牌的链接）：")
            write(f"  {server.url}")
            write("在页面点击“完成并关闭”，或在终端按 Ctrl+C 退出。")
            if not args.no_browser and open_browser is not None:

                def _launch_browser(url: str = server.url) -> None:
                    with suppress(Exception):
                        open_browser(url)

                # Launch off-thread so foreground Unix browsers cannot block shutdown or idle-timeout monitoring.
                threading.Thread(target=_launch_browser, name="mosscode-open-browser", daemon=True).start()
            try:
                server.wait_until_stopped()
            except KeyboardInterrupt:
                write("\n正在关闭本地配置服务…")
    except OSError as exc:
        err(f"moss config --web：无法启动本地配置服务：{exc}")
        return 1
    write("已关闭本地配置服务。")
    return 0
