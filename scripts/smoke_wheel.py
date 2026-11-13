"""Exercise an installed wheel without credentials, a network connection or source imports."""

import asyncio
import json
import tempfile
from pathlib import Path

import mosscode
from mosscode.catalog import load_catalog
from mosscode.config import load_settings
from mosscode.conversation import Conversation
from mosscode.messages import Completed, ContentBlock, Message, ToolCall, Usage
from mosscode.skills import SkillCatalog
from mosscode.tools import create_default_registry


class OfflineProvider:
    def __init__(self):
        self.requests = 0

    async def stream(self, messages, system, tools=(), *, allow_tools=True):
        self.requests += 1
        if self.requests == 1:
            calls = (ToolCall("read", "read_file", json.dumps({"path": "sample.txt"})),)
        elif self.requests == 2:
            calls = (
                ToolCall("edit", "edit_file", json.dumps({"path": "sample.txt", "old_text": "old", "new_text": "new"})),
            )
        else:
            calls = ()
        message = Message("assistant", (ContentBlock("text", "完成"),) if not calls else (), tool_calls=calls)
        yield Completed(message, "tool_calls" if calls else "stop", Usage(100, 10))

    async def close(self):
        pass


async def main():
    source = Path(__file__).resolve().parents[1] / "src"
    assert not Path(mosscode.__file__).resolve().is_relative_to(source), (
        "Loaded the source checkout instead of the wheel"
    )
    with tempfile.TemporaryDirectory(prefix="mosscode-wheel-") as temporary:
        root = Path(temporary)
        config = root / "fixture.yaml"
        config.write_text(
            "providers:\n- name: offline\n  protocol: openai-chat-completions\n  model: fixture\n"
            "run_budget:\n  requests: 3\n",
            encoding="utf-8",
        )
        settings = load_settings(config, home=root / "home")
        assert load_catalog(root / "home").models, "The bundled model catalog is missing"
        skills = SkillCatalog.load(root, root / "home", plugins=())
        assert {"test", "review", "commit"} <= set(skills.skills)
        assert skills.skills["test"].tools, "The test skill's script tool is missing"
        sample = root / "sample.txt"
        sample.write_text("old\n", encoding="utf-8")
        provider = OfflineProvider()
        registry = create_default_registry(root, permission_mode="bypassPermissions", load_permissions=False)
        conversation = Conversation(provider, registry=registry, skills=skills, run_budget=settings.run_budget)
        try:
            assert await conversation.ask("把 sample.txt 的 old 改为 new") == "完成"
            assert sample.read_text(encoding="utf-8") == "new\n" and provider.requests == 3
        finally:
            await conversation.close()
    print("安装包冒烟通过：配置、读写工具、共享预算和内置 Skill 资源。")


if __name__ == "__main__":
    asyncio.run(main())
