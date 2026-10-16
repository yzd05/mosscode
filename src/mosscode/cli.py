import argparse
import asyncio
from contextlib import aclosing
import json
from time import monotonic
import sys

from .config import ConfigError, find_config, load_settings
from .conversation import Conversation
from .messages import (Completed, Delta, Message, ProviderError, Retrying, ToolProgress, ResponseBoundary, RunStopped,
                       UsageUpdate)
from .usage import request_usage_text
from .tools import create_default_registry
from .tools.shell import ShellNotFound
from .permissions import PermissionConfigError, normalize_mode
from .providers import create_provider
from .tui import MossCodeApp, ProviderSelectApp, plan_text
from .mcp_config import MCPConfigError, load_mcp_settings
from .mcp_client import MCPManager
from pathlib import Path

OUTPUT_FORMATS = ("text", "json", "stream-json")


def _permission_mode(value: str) -> str:
    try:
        return normalize_mode(value)
    except PermissionConfigError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _arguments(arguments: str):
    try:
        return json.loads(arguments)
    except ValueError:
        return arguments


def _assistant(message: Message, usage, stop_reason: str | None) -> dict:
    return {"type": "assistant", "text": message.text,
            "tool_calls": [{"id": c.id, "name": c.name, "arguments": _arguments(c.arguments)} for c in message.tool_calls],
            "stop_reason": stop_reason, "usage": _usage_dict(usage)}


def _usage_dict(usage) -> dict | None:
    if usage is None:
        return None
    return {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
            "cached_input_tokens": usage.cached_input_tokens, "cache_write_tokens": usage.cache_write_tokens}


def _emit(data: dict) -> None:
    print(json.dumps(data, ensure_ascii=False), flush=True)


async def _prompt(conversation: Conversation, prompt: str, *, show_usage: bool = False,
                  output_format: str = "text", model: str = "") -> dict | None:
    """Run one headless task; JSON formats return the result object instead of raising."""
    if output_format == "text":
        await _text_prompt(conversation, prompt, show_usage=show_usage)
        return None
    stream_json = output_format == "stream-json"
    started = monotonic()
    stopped: RunStopped | None = None
    final = ""
    error: str | None = None
    try:
        try:
            notices = await conversation.start()
            for notice in notices:
                if stream_json:
                    _emit({"type": "notice", "text": notice})
                else:
                    print(notice, file=sys.stderr, flush=True)
            registry = conversation.registry
            if stream_json:
                _emit({"type": "system", "subtype": "init", "cwd": str(Path.cwd()), "model": model,
                       "permission_mode": registry.permissions.mode if registry else None,
                       "tools": [d.name for d in registry.definitions()] if registry else []})
            usage = None
            async with aclosing(conversation.reply(prompt)) as stream:
                async for event in stream:
                    if isinstance(event, UsageUpdate):
                        usage = event.usage
                    elif isinstance(event, RunStopped):
                        stopped = event
                    elif isinstance(event, Completed):
                        final = event.message.text
                    if not stream_json:
                        continue
                    if isinstance(event, ResponseBoundary):
                        _emit(_assistant(event.message, usage, "tool_use"))
                    elif isinstance(event, Completed):
                        _emit(_assistant(event.message, usage, event.stop_reason))
                    elif isinstance(event, ToolProgress) and event.phase == "finished":
                        result = event.result
                        _emit({"type": "tool_result", "id": event.call.id, "name": event.call.name,
                               "is_error": result.is_error, "code": result.code, "output": result.output})
                    elif isinstance(event, Retrying):
                        _emit({"type": "retry", "attempt": event.attempt, "max_attempts": event.max_attempts,
                               "delay": event.delay, "reason": event.reason})
        except ProviderError as exc:
            error = str(exc)
        registry = conversation.registry
        total = conversation.total_usage
        subtype = "error" if error is not None else stopped.reason if stopped else "error"
        result = {
            "type": "result", "subtype": subtype, "is_error": subtype != "completed",
            "result": final if subtype == "completed" else error or (stopped.notice if stopped else "没有完整回复。"),
            "num_turns": stopped.iterations if stopped else 0,
            "duration_ms": round((monotonic() - started) * 1000),
            "usage": {"input_tokens": total.input_tokens, "output_tokens": total.output_tokens,
                      "cached_input_tokens": total.cached_input_tokens, "cache_write_tokens": total.cache_write_tokens,
                      "requests": total.requests},
            "model": model, "permission_mode": registry.permissions.mode if registry else None,
        }
        _emit(result)
        return result
    finally:
        await conversation.close()


async def _text_prompt(conversation: Conversation, prompt: str, *, show_usage: bool = False) -> None:
    try:
        for notice in await conversation.start():
            print(notice, file=sys.stderr, flush=True)
        async with aclosing(conversation.reply(prompt)) as stream:
            async for event in stream:
                if isinstance(event, Delta) and event.kind == "text":
                    print(event.text, end="", flush=True)
                elif isinstance(event, ResponseBoundary):
                    print()
                elif isinstance(event, ToolProgress) and event.phase == "running" and event.call.name == "exit_plan_mode":
                    print(f"\n计划：\n{plan_text(event.call.arguments)}", flush=True)
                elif isinstance(event, ToolProgress) and event.phase == "finished":
                    print(f"\n● {event.call.name} · {event.elapsed:.1f}s\n└ {event.result.output}", flush=True)
                elif isinstance(event, RunStopped) and event.reason in {"max_iterations", "unknown_tools", "max_tokens"}:
                    print("\n" + event.notice, flush=True)
                elif isinstance(event, Retrying):
                    print(f"{event.reason}，{event.delay:.1f} 秒后重试（{event.attempt}/{event.max_attempts}）",
                          file=sys.stderr, flush=True)
                elif isinstance(event, UsageUpdate) and show_usage:
                    print(request_usage_text(event.usage, event.iteration), file=sys.stderr, flush=True)
        print()
    finally:
        await conversation.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="MossCode：Agent Loop、工具权限与流式对话（官方及兼容端点）")
    parser.add_argument("--config", help="YAML 配置路径；默认依次查找 .mosscode/config.yaml、config.yaml、~/.mosscode/config.yaml")
    parser.add_argument("--provider", help="指定命名 Provider，跳过启动选择；无界面模式默认使用 default_provider")
    parser.add_argument("-p", "--prompt", help="无界面执行一次任务并输出结果")
    parser.add_argument("--output-format", choices=OUTPUT_FORMATS, default="text",
                        help="无界面输出格式：text（默认，流式正文）、json（结束时输出一个结果对象）、stream-json（每行一个 JSON 事件）")
    parser.add_argument("--show-usage", action="store_true", help="text 格式在 stderr 显示逐次请求的 Token 与缓存用量")
    parser.add_argument("--list-providers", action="store_true", help="列出配置，不发请求")
    parser.add_argument("--permission-mode", type=_permission_mode, metavar="MODE", help="权限档位：default/acceptEdits/plan/bypassPermissions；仍遵守硬限制和显式拒绝")
    args = parser.parse_args()
    if args.output_format != "text" and args.prompt is None:
        parser.error("--output-format 只用于 -p/--prompt 无界面模式。")
    try:
        settings = load_settings(args.config if args.config is not None else find_config())
        if args.list_providers:
            for item in settings.providers:
                print(f"{item.name}: {item.protocol} / {item.model}")
            return
        config = settings.select(args.provider)
        if (
            args.prompt is None and args.provider is None
            and settings.default_provider is None and len(settings.providers) > 1
        ):
            config = ProviderSelectApp(settings).run(inline=True, mouse=False)
            if config is None:
                return
        registry = create_default_registry(permission_mode=args.permission_mode, shell=settings.shell)
        mcp_settings = load_mcp_settings(Path.cwd())
        manager = MCPManager(Path.cwd(), mcp_settings) if mcp_settings.servers or mcp_settings.notices else None
        conversation = Conversation(create_provider(config), settings.system_prompt, registry, mcp_manager=manager,
                                    max_iterations=settings.max_iterations, context_root=Path.cwd())
        if args.prompt is not None:
            if args.output_format != "text" and hasattr(sys.stdout, "reconfigure"):
                # Machine-readable output is always UTF-8, whatever the console code page.
                sys.stdout.reconfigure(encoding="utf-8")
            result = asyncio.run(_prompt(conversation, args.prompt, show_usage=args.show_usage,
                                         output_format=args.output_format, model=config.model))
            if result is not None and result["is_error"]:
                raise SystemExit(1)
        else:
            MossCodeApp(conversation, config.name, settings=settings, model=config.model).run(
                inline=True, inline_no_clear=True, mouse=False,
            )
    except (ConfigError, ProviderError, PermissionConfigError, MCPConfigError, ShellNotFound) as exc:
        print(f"MossCode: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        raise SystemExit(130) from None
