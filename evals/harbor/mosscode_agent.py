"""Harbor 0.26 installed adapter. The task environment owns execution and verification."""

import asyncio
import hashlib
import json
import shlex
from pathlib import Path

from evals.harbor.results import parse_stream, read_json
from harbor.agents.agent_auth_resolution import ModelConnectionSpec, ProviderAccess
from harbor.agents.capabilities import AgentCapabilities
from harbor.agents.installed.base import ApiError, BaseInstalledAgent, NetworkConnectionError, NonZeroAgentExitCodeError
from harbor.models.model_api_format import ModelApiFormat

ROOT = "/installed-agent/mosscode"
UV_VERSION = "0.11.18"
PYTHON_VERSION = "3.12.13"
FEATURES = {"auto_compact", "context_collapse", "command_auto_background", "max_iterations", "run_budget", "todo_tool"}


class MossCodeAgent(BaseInstalledAgent):
    capabilities = AgentCapabilities(base_url=True)
    MODEL_CONNECTION = ModelConnectionSpec(
        default_provider="deepseek",
        agent_provider_access=ProviderAccess(api_key_candidate_names=["DEEPSEEK_API_KEY"]),
        api_key_destination_names={"default": "DEEPSEEK_API_KEY"},
        base_url_destination_names={"default": "MOSSCODE_EVAL_BASE_URL"},
        supported_api_formats=[ModelApiFormat.OPENAI_CHAT_COMPLETIONS],
        default_api_format_by_provider={"default": ModelApiFormat.OPENAI_CHAT_COMPLETIONS},
    )

    def __init__(
        self,
        logs_dir,
        *,
        wheel,
        label="candidate",
        features=None,
        prices=None,
        max_output_tokens=16384,
        context_window=128000,
        finish_commands=(),
        **kwargs,
    ):
        self.wheel = Path(wheel).resolve()
        self.build = read_json(self.wheel.with_suffix(".json"))
        if hashlib.sha256(self.wheel.read_bytes()).hexdigest() != self.build["wheel_sha256"]:
            raise ValueError("MossCode wheel 与构建记录不一致")
        self.features = features or {}
        if set(self.features) - FEATURES:
            raise ValueError("不支持的评估功能开关")
        self.prices, self.label = prices, label
        self.max_output_tokens, self.context_window = max_output_tokens, context_window
        self.finish_commands = (finish_commands,) if isinstance(finish_commands, str) else tuple(finish_commands)
        super().__init__(logs_dir, **kwargs)

    @staticmethod
    def name():
        return "mosscode"

    def version(self):
        return self.build["revision"]

    async def install(self, environment):
        for attempt in range(2):
            try:
                await self.ensure_system_dependencies(environment, ("curl", "ca_certificates"))
                break
            except (NetworkConnectionError, NonZeroAgentExitCodeError):
                if attempt == 1:
                    raise
                await asyncio.sleep(2 * (attempt + 1))
        await self.exec_as_root(environment, f"mkdir -p {ROOT}")
        await environment.upload_file(self.wheel, f"{ROOT}/{self.wheel.name}")
        # A managed interpreter and venv leave the task's Python and dependencies alone.
        await self.exec_as_root(
            environment,
            f"curl --retry 2 -fsSL -o /tmp/uv-install.sh https://astral.sh/uv/{UV_VERSION}/install.sh && "
            f"env UV_INSTALL_DIR={ROOT}/bin UV_NO_MODIFY_PATH=1 sh /tmp/uv-install.sh && "
            "rm -f /tmp/uv-install.sh",
        )
        await self.exec_as_root(
            environment,
            f"{ROOT}/bin/uv venv --python {PYTHON_VERSION} {ROOT}/venv && "
            f"{ROOT}/bin/uv pip install --python {ROOT}/venv/bin/python {shlex.quote(ROOT + '/' + self.wheel.name)} && "
            f"chmod -R a+rX {ROOT}",
            env={"UV_PYTHON_INSTALL_DIR": f"{ROOT}/python", "UV_CACHE_DIR": f"{ROOT}/cache"},
        )

    async def configure(self, environment):
        if self.mcp_servers or self.skills_dir:
            raise ValueError("本评估配置不注入 MCP 或自定义 Skill")
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        config = {
            "default_provider": "eval",
            "memory": False,
            "auto_memory": False,
            "persist_sessions": False,
            "coordinator_mode": False,
            **self.features,
            "providers": [
                {
                    "name": "eval",
                    "protocol": "openai-chat-completions",
                    "model": self._parsed_model_name,
                    "base_url": self.model_connection.resolved_base_url,
                    "api_key_env": "DEEPSEEK_API_KEY",
                    "stream_usage": True,
                    "max_output_tokens": self.max_output_tokens,
                    "context_window": self.context_window,
                }
            ],
        }
        local = self.logs_dir / "moss-config.json"
        local.write_text(json.dumps(config), encoding="utf-8")
        await environment.upload_file(local, f"{ROOT}/config.json")
        await self.exec_as_agent(environment, "mkdir -p /tmp/mosscode-eval-home/.mosscode")
        if self.finish_commands:
            hooks = {
                "hooks": [
                    {
                        "id": f"visible-check-{i}",
                        "event": "pre_finish",
                        "action": {"type": "command", "command": command},
                    }
                    for i, command in enumerate(self.finish_commands)
                ]
            }
            local = self.logs_dir / "visible-hooks.json"
            local.write_text(json.dumps(hooks), encoding="utf-8")
            await environment.upload_file(local, "/tmp/mosscode-eval-home/.mosscode/hooks.yaml")
        await self.exec_as_root(environment, f"chmod -R a+rX {ROOT} /tmp/mosscode-eval-home")
        await self.exec_as_agent(
            environment,
            f"{ROOT}/bin/uv pip freeze --python {ROOT}/venv/bin/python > "
            f"{shlex.quote(str(self.environment_logs_dir) + '/dependencies.txt')}",
        )

    async def run(self, instruction, environment, context):
        local = self.logs_dir / "instruction.txt"
        # Leading "/" is escaped as "//" so benchmark instructions never trigger -p slash-command parsing.
        prompt = "/" + instruction.lstrip() if instruction.lstrip().startswith("/") else instruction
        local.write_text(prompt, encoding="utf-8")
        await environment.upload_file(local, f"{ROOT}/instruction.txt")
        await self.exec_as_root(environment, f"chmod a+r {ROOT}/instruction.txt")
        logs = str(self.environment_logs_dir)
        command = (
            f"{ROOT}/venv/bin/moss --config {ROOT}/config.json --trust-project-config "
            f"--permission-mode bypassPermissions --output-format stream-json "
            f"{'--no-hooks ' if not self.finish_commands else ''}-p < {ROOT}/instruction.txt "
            f"> {shlex.quote(logs + '/moss.jsonl')} 2> {shlex.quote(logs + '/stderr.log')}; "
            f"echo $? > {shlex.quote(logs + '/exit-code')}"
        )
        try:
            await self.exec_as_agent(
                environment,
                command,
                env={
                    "HOME": "/tmp/mosscode-eval-home",
                    "XDG_CONFIG_HOME": "/tmp/mosscode-eval-home/.config",
                    "DEEPSEEK_API_KEY": self.model_connection.api_key or "",
                },
            )
        finally:
            for name in ("moss.jsonl", "stderr.log", "exit-code"):
                try:
                    await asyncio.shield(environment.download_file(f"{logs}/{name}", self.logs_dir / name))
                except BaseException:
                    pass
            self.populate_context_post_run(context)
        stop = context.metadata["mosscode"]["stop"]
        if stop == "error":
            raise ApiError("MossCode 模型请求失败；详情保留在私有 Agent 日志")
        if stop == "missing_result":
            raise NonZeroAgentExitCodeError("MossCode 没有完整结果；详情保留在私有 Agent 日志")

    def populate_context_post_run(self, context):
        path = self.logs_dir / "moss.jsonl"
        info = parse_stream(path.read_text(encoding="utf-8"), self.prices) if path.exists() else parse_stream("")
        info.update({"revision": self.build["revision"], "wheel_sha256": self.build["wheel_sha256"]})
        context.n_input_tokens = info["input_tokens"]
        context.n_output_tokens = info["output_tokens"]
        context.n_cache_tokens = info["cached_input_tokens"]
        context.cost_usd = info["cost_usd"]
        context.metadata = {**(context.metadata or {}), "mosscode": info}
