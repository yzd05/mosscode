"""Skill tools: load_skill pins an SOP; a skill's own tools run its scripts."""

import json
import os
import shutil
import sys
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path

from pydantic import Field

from ..messages import ToolDefinition
from .base import Params, Tool, ToolFailure
from .process import COMMAND_HEAD, COMMAND_TAIL, run_process

FAILURE_TAIL = 2000  # Characters of stdout and stderr kept in a failure message.


class LoadSkillParams(Params):
    name: str = Field(min_length=1, max_length=64)
    arguments: str = Field(default="", max_length=4000)


class LoadSkill(Tool):
    name = "load_skill"
    permission_kind = "load_skill"  # No approval; it only reads the skill's own files.
    read_only = True  # Offered in plan mode as well.
    description = (
        "加载一个 Skill（可复用的操作规程）。系统提示的 Skill 一节列出了可加载的名称和描述；"
        "用户的请求与某个描述相符时，先调用本工具，再按加载后的规程执行。"
        "加载后规程出现在 <active-skills> 中并在本会话中持续有效，工具结果不重复规程全文；"
        "Skill 的专属工具（skill__ 开头）同时变为可用。arguments 是传给规程的参数，可省略。"
    )
    prompt_summary = "加载与请求相符的 Skill 规程"
    prompt_guidelines = (
        "只加载 Skill 一节中列出的名称；已在 <active-skills> 中的 Skill 不需要再次加载，除非要换用新的参数。",
    )
    params_model = LoadSkillParams

    def __init__(self, root, loader: Callable[[str, str], tuple[str, dict]]):
        super().__init__(root)
        self.loader = loader

    async def execute(self, params: LoadSkillParams):
        return self.loader(params.name, params.arguments)


def _tail(text: str) -> str:
    return text if len(text) <= FAILURE_TAIL else "…" + text[-FAILURE_TAIL:]


class SkillTool(Tool):
    """A tool from tool.json; argv without a shell, arguments as JSON on stdin, approval every call."""

    permission_kind = "skill"
    read_only = False

    def __init__(self, root: Path, skill, spec):
        from ..mcp_client import check_schema

        super().__init__(root)
        self.skill_name, self.short_name = skill.name, spec.name
        self.directory = skill.directory.resolve()
        self.name = spec.full_name
        self.description = f"Skill {skill.name} 的专属工具。{spec.description}"
        self.prompt_summary = f"Skill {skill.name} 的专属工具 {spec.name}"
        self.schema = deepcopy(spec.input_schema)
        self.validator = check_schema(self.schema)
        self.command = spec.command
        self.timeout = spec.timeout
        self.plugin = skill.plugin  # A plugin's skill: its ${CLAUDE_PLUGIN_ROOT} and friends, and their variables.

    def definition(self) -> ToolDefinition:
        return ToolDefinition(self.name, self.description, deepcopy(self.schema), self.prompt_summary)

    def validate_arguments(self, raw):
        from ..mcp_client import json_arguments

        return json_arguments(self.validator, raw, "Skill 工具")

    def argv(self) -> list[str]:
        """${SKILL_DIR} and ${PYTHON} substituted, and a plugin's variables."""
        argv = [
            part.replace("${SKILL_DIR}", str(self.directory)).replace("${PYTHON}", sys.executable)
            for part in self.command
        ]
        if self.plugin is None:
            return argv
        from ..plugins import PluginError

        try:
            return [self.plugin.substitute(part, self.root) for part in argv]
        except PluginError as exc:
            raise ToolFailure("plugin_config", f"{exc}。未执行。") from None

    def _executable(self, first: str) -> str:
        if os.sep in first or "/" in first:
            if Path(first).is_file():
                return first
        elif found := shutil.which(first):
            return found
        raise ToolFailure("command_not_found", f"找不到 Skill 工具的命令：{first}。未执行。")

    async def execute(self, params):
        argv = self.argv()
        argv[0] = self._executable(argv[0])
        env = {
            **os.environ,
            "MOSSCODE_SKILL_DIR": str(self.directory),
            "MOSSCODE_PROJECT_ROOT": str(self.root),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            **(self.plugin.env(self.root) if self.plugin is not None else {}),
        }
        data = json.dumps(params.model_dump(), ensure_ascii=False).encode("utf-8")
        details = await run_process(
            argv, self.root, check=False, limit=COMMAND_HEAD, tail=COMMAND_TAIL, env=env, input=data
        )
        details = {**details, "skill": self.skill_name, "tool": self.short_name}
        if details["exit_code"] != 0:
            parts = [f"Skill 工具 {self.name} 退出码为 {details['exit_code']}。"]
            if details["stdout"]:
                parts.append("stdout 末尾：\n" + _tail(details["stdout"]))
            if details["stderr"]:
                parts.append("stderr 末尾：\n" + _tail(details["stderr"]))
            raise ToolFailure("nonzero_exit", "\n".join(parts), **details)
        return details["stdout"] or "Skill 工具完成，无输出。", details
