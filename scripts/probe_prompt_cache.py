"""Explicit live cache probe: requests a real service, never executes returned tools."""

import argparse
import asyncio
import json
from contextlib import aclosing
from dataclasses import asdict
from pathlib import Path
from time import monotonic

from mosscode.config import ConfigError, load_settings
from mosscode.messages import Completed, ContentBlock, Message, ProviderError
from mosscode.prompts import PromptRequest, Supplement, build_system_prompt, environment, mode_reminder
from mosscode.providers import create_provider
from mosscode.tools import create_default_registry


async def probe(config_path: str, provider_name: str | None):
    settings = load_settings(config_path)
    config = settings.select(provider_name)
    provider = create_provider(config)
    root = Path.cwd()
    definitions = create_default_registry(
        root, permission_mode="bypassPermissions", load_permissions=False
    ).definitions()
    system = build_system_prompt(definitions, override=settings.system_prompt)
    env = environment(root, tools_enabled=True)
    reminder = mode_reminder("normal", 1)
    message = Message("user", (ContentBlock("text", "只回复两个字：收到。不要调用工具。"),))
    records = []
    try:
        for index in range(1, 4):
            # First two payloads are identical; the third changes only an uncached supplement.
            extras = (Supplement("probe-context", "这是缓存观测请求，不需要解释。"),) if index == 3 else ()
            prompt = PromptRequest(system, (env, reminder) + extras)
            started = monotonic()
            final = None
            async with aclosing(provider.stream((message,), prompt, definitions, allow_tools=False)) as stream:
                async for event in stream:
                    if isinstance(event, Completed):
                        final = event
            if final is None:
                raise ProviderError("缓存探测未获得完整响应。")
            read = final.usage.cached_input_tokens
            records.append(
                {
                    "request": index,
                    "protocol": config.protocol,
                    "elapsed_seconds": round(monotonic() - started, 3),
                    "cache_observation": "unreported" if read is None else "hit" if read > 0 else "miss",
                    **asdict(final.usage),
                }
            )
            # Tool definitions take part in prefix matching; no returned call is executed.
            print(json.dumps(records[-1], ensure_ascii=False), flush=True)
        return records
    finally:
        await provider.close()


def main():
    parser = argparse.ArgumentParser(
        description="发送 3 次真实 API 请求，只观测缓存；不执行工具，不输出提示、密钥或回复正文"
    )
    parser.add_argument("--config", required=True, help="明确指定真实配置路径")
    parser.add_argument("--provider", help="Provider 配置名称；省略时遵循配置默认值")
    parser.add_argument("--output", type=Path, help="可选：保存纯计数 JSON")
    args = parser.parse_args()
    try:
        records = asyncio.run(probe(args.config, args.provider))
        if args.output:
            args.output.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (ConfigError, ProviderError) as exc:
        parser.exit(1, f"MossCode: {exc}\n")
    except KeyboardInterrupt:
        parser.exit(130, "已取消缓存探测。\n")


if __name__ == "__main__":
    main()
