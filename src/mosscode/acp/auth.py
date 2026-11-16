"""Authentication descriptors and local credential checks for ACP."""

from __future__ import annotations

import os
from pathlib import Path

from acp import RequestError
from acp.schema import AuthenticateResponse, AuthMethodAgent, ClientCapabilities, TerminalAuthMethod

from ..config import (
    ConfigError,
    ProviderConfig,
    Settings,
    config_candidates,
    find_config,
    is_project_file,
    load_settings,
    project_confirmed,
    project_notice,
    refreshed,
)
from ..credentials import SECRET_HEADER, KeyringError, hide_variable, keyring_get, plaintext_notice, remember

TERMINAL_AUTH_ID = "moss-setup-terminal"
LOCAL_CONFIG_AUTH_ID = "local-config"
AUTH_METHOD_IDS = (TERMINAL_AUTH_ID, LOCAL_CONFIG_AUTH_ID)
TERMINAL_AUTH_META = {
    "terminal-auth": {
        "command": "moss",
        "args": ["setup"],
        "label": "运行 moss setup 配置向导",
    }
}


def build_auth_methods(
    client_capabilities: ClientCapabilities | None = None,
) -> list[TerminalAuthMethod | AuthMethodAgent]:
    """Return ACP authMethods negotiated against the client's declared capabilities."""
    methods: list[TerminalAuthMethod | AuthMethodAgent] = []
    supports_terminal = bool(
        client_capabilities is not None and client_capabilities.auth is not None and client_capabilities.auth.terminal
    )
    if supports_terminal:
        methods.append(
            TerminalAuthMethod(
                id=TERMINAL_AUTH_ID,
                name="运行 MossCode 交互配置向导",
                description="在终端运行 moss setup 配置模型协议与 API 密钥",
                args=["setup"],
                field_meta=dict(TERMINAL_AUTH_META),
            )
        )
    methods.append(
        AuthMethodAgent(
            id=LOCAL_CONFIG_AUTH_ID,
            name="使用本地配置与凭据",
            description="读取 ~/.mosscode/config.yaml、环境变量、系统凭据管理器或已登录的 Claude Code / Codex 凭据",
            field_meta=dict(TERMINAL_AUTH_META),
        )
    )
    return methods


def remember_settings_credentials(settings: Settings) -> None:
    """Register API keys from settings for redaction and env hiding without mutating global command_env."""
    for config in (*settings.providers, *((settings.web_search,) if settings.web_search else ())):
        hide_variable(config.api_key_env)
        if config.api_key_env:
            remember(os.environ.get(config.api_key_env))
        remember(config.api_key)
        if config.api_key_keyring:
            try:
                remember(keyring_get(config.api_key_keyring))
            except KeyringError:
                pass
        for header, value in getattr(config, "headers", {}).items():
            if SECRET_HEADER.search(header):
                remember(value)


def resolve_auth_cwd(
    cwd: Path | None,
    *,
    trust_project_config: bool = False,
    home: Path | None = None,
) -> Path:
    """Pick a safe config lookup directory when no explicit session cwd is available."""
    if cwd is not None:
        return cwd
    fallback_home = (home or Path.home()).expanduser()
    try:
        proc_cwd = Path.cwd()
    except OSError:
        return fallback_home
    candidates = config_candidates(cwd=proc_cwd, home=home)
    proj_cfg, user_cfg = candidates[0], candidates[-1]
    if proj_cfg != user_cfg and proj_cfg.is_file():
        try:
            loaded = load_settings(proj_cfg, home=home)
        except ConfigError:
            return fallback_home
        if trust_project_config or project_confirmed(loaded, home=home):
            return proc_cwd
    return fallback_home


def load_session_settings(
    cwd: Path,
    *,
    explicit_config: str | Path | None = None,
    provider_name: str | None = None,
    trust_project_config: bool = False,
    home: Path | None = None,
    settings: Settings | None = None,
) -> tuple[Settings, ProviderConfig, list[str]]:
    """Load Settings and validate the selected ProviderConfig credential for an ACP session.

    Raises ``acp.RequestError.auth_required`` (-32000) when configuration or credentials
    are missing or invalid.
    """
    notices: list[str] = []
    try:
        if settings is None:
            if explicit_config is not None:
                path = Path(explicit_config).expanduser()
                settings = load_settings(path, home=home)
                if (
                    not trust_project_config
                    and is_project_file(path, home)
                    and not project_confirmed(settings, home=home)
                ):
                    raise ConfigError(
                        f"项目配置 {settings.source} 尚未确认；请先在终端运行 moss 确认，或传入 --trust-project-config。"
                    )
            else:
                path = find_config(cwd=cwd, home=home)
                settings = load_settings(path, home=home)
                if not trust_project_config and not project_confirmed(settings, home=home):
                    notices.append(
                        f"未加载未经确认的项目配置 {settings.source}（可在终端运行 moss 确认，或传入 --trust-project-config）。"
                    )
                    user_config = config_candidates(cwd=cwd, home=home)[-1]
                    if user_config == path or not user_config.is_file():
                        raise ConfigError(
                            f"项目配置 {settings.source} 尚未确认，且未找到用户级配置 {user_config}；请先运行 moss setup。"
                        )
                    settings = load_settings(user_config, home=home)
        if note := project_notice(settings):
            notices.append(note)
        if note := plaintext_notice(settings, cwd):
            notices.append(note)
        remember_settings_credentials(settings)
        target_name = provider_name or settings.default_provider or settings.providers[0].name
        provider_config = refreshed(settings.select(target_name), home=home)
        provider_config.credential()
    except ConfigError as exc:
        raise RequestError.auth_required({"reason": str(exc)}) from None
    return settings, provider_config, notices


def authenticate_method(
    method_id: str,
    cwd: Path | None = None,
    *,
    explicit_config: str | Path | None = None,
    provider_name: str | None = None,
    trust_project_config: bool = False,
    home: Path | None = None,
    settings: Settings | None = None,
) -> AuthenticateResponse:
    """Validate an ACP authenticate request and verify local credentials."""
    if method_id not in AUTH_METHOD_IDS:
        raise RequestError.invalid_params({"methodId": method_id, "reason": f"不支持的认证方式：{method_id}"})
    target_cwd = resolve_auth_cwd(cwd, trust_project_config=trust_project_config, home=home)
    load_session_settings(
        target_cwd,
        explicit_config=explicit_config,
        provider_name=provider_name,
        trust_project_config=trust_project_config,
        home=home,
        settings=settings,
    )
    return AuthenticateResponse()
