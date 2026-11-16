"""Command-line entry point for `moss acp`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import acp

from ..launcher import _permission_mode, program
from .app import MossCodeAcpAgent
from .session import AcpLaunchOptions


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"{program()} acp",
        description="启动 MossCode Agent Client Protocol (ACP) 服务，通过 stdio (JSON-RPC 2.0) 与编辑器通信。",
    )
    parser.add_argument(
        "--config",
        help="YAML 配置路径；默认按会话工作目录查找 .mosscode/config.yaml 或 ~/.mosscode/config.yaml",
    )
    parser.add_argument(
        "--provider",
        help="指定初始 Provider 名称；默认使用配置中的 default_provider 或第一个 Provider",
    )
    parser.add_argument(
        "--permission-mode",
        type=_permission_mode,
        metavar="MODE",
        help="初始权限模式：default/acceptEdits/plan/bypassPermissions",
    )
    parser.add_argument(
        "--no-hooks",
        action="store_true",
        help="本次运行不加载任何 Hook",
    )
    parser.add_argument(
        "--sandbox",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="本次运行打开（--sandbox）或关闭（--no-sandbox）命令沙箱，覆盖配置中的 sandbox.mode",
    )
    parser.add_argument(
        "--trust-project-config",
        action="store_true",
        help="本次运行信任项目级 .mosscode/config.yaml",
    )
    parser.add_argument(
        "--trust-project-mcp",
        action="store_true",
        help="本次运行信任项目级 .mosscode/mcp.yaml 和 .mcp.json",
    )
    parser.add_argument(
        "--trust-project-hooks",
        action="store_true",
        help="本次运行信任项目级 Hook 文件",
    )
    parser.add_argument(
        "--trust-project-plugins",
        action="store_true",
        help="本次运行信任项目声明的插件",
    )
    return parser


def parse_acp_args(argv: list[str] | None = None) -> AcpLaunchOptions:
    args = build_parser().parse_args(argv)
    return AcpLaunchOptions(
        config=args.config,
        provider=args.provider,
        permission_mode=args.permission_mode,
        no_hooks=args.no_hooks,
        sandbox=args.sandbox,
        trust_project_config=args.trust_project_config,
        trust_project_mcp=args.trust_project_mcp,
        trust_project_hooks=args.trust_project_hooks,
        trust_project_plugins=args.trust_project_plugins,
    )


async def serve_acp(options: AcpLaunchOptions) -> None:
    agent = MossCodeAcpAgent(options)
    try:
        await acp.run_agent(agent)
    finally:
        await agent.close_all()


def main(argv: list[str] | None = None) -> int:
    options = parse_acp_args(argv)
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    try:
        asyncio.run(serve_acp(options))
    except KeyboardInterrupt:
        return 130
    return 0
