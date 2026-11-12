"""Live cache probe over a real multi-turn tool session: prints token counts only.

Runs a short coding task (a checklist, reads, edits) against a real service in a throwaway project, the way the
interface would, and reports each request's input and cached tokens. Commands are denied; no prompt, key or reply
text is printed.
"""

import argparse
import asyncio
import json
import shutil
import tempfile
from contextlib import aclosing
from pathlib import Path

from mosscode.config import ConfigError, find_config, load_settings
from mosscode.conversation import Conversation
from mosscode.messages import ProviderError, UsageUpdate
from mosscode.permissions import ApprovalRequest
from mosscode.providers import create_provider
from mosscode.tools import create_default_registry

FILES = {
    "calc.py": "def add(a, b):\n    return a - b\n\n\ndef sub(a, b):\n    return a - b\n",
    "test_calc.py": "from calc import add, sub\n\n\ndef test_add():\n    assert add(2, 3) == 5\n\n\n"
    "def test_sub():\n    assert sub(5, 3) == 2\n",
    "notes.md": "# 修改记录\n",
}

TURNS = (
    "先用 todo_write 列出三步清单：读 calc.py、修复 add 的错误、在 notes.md 末尾记一行修复说明。"
    "然后逐步完成，每完成一步就更新清单。不要运行命令。",
    "再给 calc.py 加一个 mul 函数，并在 test_calc.py 加一个对应的测试，同样用清单跟踪。不要运行命令。",
    "读一下 notes.md 和 calc.py，用一句话说明这次改了什么。",
    "calc.py 现在有几个函数？只回答数字。",
)


def rate(rows) -> float | None:
    total = sum(r["input_tokens"] or 0 for r in rows)
    cached = sum(r["cached_input_tokens"] or 0 for r in rows)
    return round(cached / total, 4) if total else None


async def probe(config_path: Path, provider_name: str | None, label: str):
    settings = load_settings(config_path)
    config = settings.select(provider_name)
    root = Path(tempfile.mkdtemp(prefix="mosscode-cache-"))
    for name, text in FILES.items():
        (root / name).write_text(text, encoding="utf-8")
    registry = create_default_registry(root, permission_mode="acceptEdits", load_permissions=False)
    conv = Conversation(create_provider(config), settings.system_prompt, registry, context_root=root)
    rows = []
    try:
        for turn, prompt in enumerate(TURNS, 1):
            async with aclosing(conv.reply(prompt)) as stream:
                async for event in stream:
                    if isinstance(event, ApprovalRequest):
                        registry.permissions.respond(event.id, "deny")
                    elif isinstance(event, UsageUpdate):
                        usage = event.usage
                        rows.append(
                            {
                                "run": label,
                                "turn": turn,
                                "iteration": event.iteration,
                                "input_tokens": usage.input_tokens,
                                "cached_input_tokens": usage.cached_input_tokens,
                                "output_tokens": usage.output_tokens,
                            }
                        )
                        print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
    finally:
        await conv.close()
        shutil.rmtree(root, ignore_errors=True)
    summary = {
        "run": label,
        "protocol": config.protocol,
        "requests": len(rows),
        "hit_rate": rate(rows),
        # The first request can only hit what another session left behind.
        "hit_rate_after_first": rate(rows[1:]),
    }
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return rows, summary


def main():
    parser = argparse.ArgumentParser(description="用真实服务跑一段多轮工具任务，只输出每次请求的 Token 与缓存计数")
    parser.add_argument("--config", type=Path, help="配置路径；省略时与 moss 相同的查找顺序")
    parser.add_argument("--provider", help="Provider 配置名称；省略时遵循配置默认值")
    parser.add_argument("--label", default="current", help="写入每行输出的标记，用于对比两次运行")
    parser.add_argument("--output", type=Path, help="可选：保存计数 JSON")
    args = parser.parse_args()
    try:
        rows, summary = asyncio.run(probe(args.config or find_config(), args.provider, args.label))
    except (ConfigError, ProviderError) as exc:
        parser.exit(1, f"MossCode: {exc}\n")
    except KeyboardInterrupt:
        parser.exit(130, "已取消缓存探测。\n")
    if args.output:
        args.output.write_text(json.dumps({"rows": rows, "summary": summary}, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
