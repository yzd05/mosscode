"""`moss setup`, and the first interactive run without a configuration.

Plain questions in the terminal (input and getpass), so it works in any console and before Textual loads.
"""

from __future__ import annotations

import argparse
import getpass
import ipaddress
import os
import re
import secrets
import sys
from collections.abc import Callable, Collection
from contextlib import ExitStack, suppress
from pathlib import Path
from urllib.parse import urlsplit

from filelock import FileLock
from filelock import Timeout as FileLockTimeout

from .config import OFFICIAL as OFFICIAL
from .model_discovery import (
    CONFIG_LOCK_TIMEOUT,
    allocate_keyring_name,
    config_lock_path,
    keyring_lock_path,
    rollback_keyring_entry,
    write_private_backup,
    write_temp_file,
)
from .model_discovery import insert_provider_text as _insert_provider_text

PROTOCOLS = (
    ("anthropic-messages", "Anthropic Messages（Claude，或兼容 Messages 的服务）"),
    ("openai-responses", "OpenAI Responses"),
    ("openai-chat-completions", "OpenAI Chat Completions（DeepSeek、Qwen 等兼容服务）"),
)
NAMES = {"anthropic-messages": "claude", "openai-responses": "openai", "openai-chat-completions": "compat"}
KEYS = {
    "anthropic-messages": "ANTHROPIC_API_KEY",
    "openai-responses": "OPENAI_API_KEY",
    "openai-chat-completions": "OPENAI_API_KEY",
}
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
PLAINTEXT_WARNING = "密钥会以明文写入配置文件，能读取这个文件的程序和人都能看到它。"


class Cancelled(Exception):
    pass


class SystemVault:
    """The system credential manager, as the wizard uses it; tests pass their own."""

    def problem(self) -> str | None:
        from .credentials import keyring_problem

        return keyring_problem()

    def label(self) -> str:
        from .credentials import keyring_label

        return keyring_label()

    def store(self, name: str, key: str) -> None:
        from .credentials import keyring_set

        keyring_set(name, key)

    def exists(self, name: str) -> bool:
        from .credentials import keyring_get

        return keyring_get(name) is not None

    def delete(self, name: str) -> None:
        from .credentials import keyring_delete

        keyring_delete(name)


def user_config(home: Path | None = None) -> Path:
    return (Path.home() if home is None else home) / ".mosscode" / "config.yaml"


def _host_name(base_url: str | None) -> str | None:
    """api.deepseek.com -> deepseek; None for an address or a single label."""
    host = urlsplit(base_url).hostname if base_url else None
    if not host:
        return None
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    labels = [label for label in host.split(".") if label]
    return labels[-2].lower() if len(labels) >= 2 and re.fullmatch(r"[a-z0-9-]+", labels[-2].lower()) else None


def _url_problem(value: str) -> str | None:
    try:
        parts = urlsplit(value)
    except ValueError:
        return "请填写 http:// 或 https:// 开头的有效地址。"
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return "请填写 http:// 或 https:// 开头的地址。"
    if parts.username or parts.password:
        return "地址中不能包含账号或密码。"
    return None


def _window(value: str) -> int | None:
    """200000, 200k or 1m; None when it is not a positive count."""
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kKmM]?)", value.replace(",", "").replace("_", ""))
    if match is None:
        return None
    number = float(match.group(1)) * {"": 1, "k": 1000, "m": 1000000}[match.group(2).lower()]
    return int(number) if number >= 1 and number == int(number) else None


def _unique_name(base: str, existing: Collection[str]) -> str:
    if base not in existing:
        return base
    suffix = 2
    while f"{base}-{suffix}" in existing:
        suffix += 1
    return f"{base}-{suffix}"


class Wizard:
    def __init__(
        self,
        read: Callable[[str], str] = input,
        secret: Callable[[str], str] = getpass.getpass,
        write: Callable[[str], None] = print,
        environ=None,
        vault=None,
    ):
        self.read, self.secret, self.write = read, secret, write
        self.environ = os.environ if environ is None else environ
        self.vault = SystemVault() if vault is None else vault
        self.pending_key: str | None = None  # For the credential manager, saved only once the user confirms.

    def ask(self, question: str, default: str = "") -> str:
        try:
            answer = self.read(f"{question}{f' [{default}]' if default else ''}：")
        except EOFError:
            raise Cancelled from None
        return answer.strip() or default

    def choose(self, question: str, options: list[str], default: int = 1) -> int:
        for index, option in enumerate(options, 1):
            self.write(f"  {index}. {option}")
        while True:
            answer = self.ask(question, str(default))
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return int(answer)
            self.write(f"  请输入 1–{len(options)} 之间的数字。")

    def collect(
        self,
        home: Path | None = None,
        *,
        existing_names: Collection[str] = (),
        default: bool = True,
    ) -> dict | None:
        """The provider entry, or None when the user did not confirm. Raises Cancelled on Ctrl+D."""
        from .follow import SOURCES, detected
        from .presets import PRESETS

        # Claude Code's or Codex's settings that already work (CC Switch and Magpie write them) come first.
        choices = [
            ("follow", source, f"跟随 {SOURCES[source]} 的设置（在 CC Switch、Magpie 中切换后随之切换）")
            for source in detected(home, self.environ)
        ]
        choices += [("preset", key, preset.label) for key, preset in PRESETS.items()]
        choices.append(("custom", "", "其他服务（手动填写协议、地址和模型）"))
        self.write("")
        kind, key, _ = choices[self.choose("选择模型服务", [label for *_, label in choices]) - 1]
        if kind == "follow":
            return self._follow(key, home, existing_names=existing_names, default=default)
        if kind == "preset":
            return self._preset(PRESETS[key], existing_names=existing_names, default=default)
        return self._custom(existing_names=existing_names, default=default)

    def _follow(
        self,
        source: str,
        home: Path | None,
        *,
        existing_names: Collection[str] = (),
        default: bool = True,
    ) -> dict | None:
        from .follow import SOURCES, resolve, settings_path

        name = _unique_name(source, existing_names)
        provider = {"name": name, "follow": source}
        found = resolve(provider, home, self.environ)
        role = "默认 Provider" if default else f"追加，可用 /model {name} 切换"
        self.write("")
        self.write("将写入：")
        for line in (
            f"名称 {name}（{role}）",
            f"跟随 {SOURCES[source]} 的设置 {settings_path(source, home, self.environ)}",
            f"现在是 {found['protocol']} / {found['model']}，"
            f"服务地址 {found.get('base_url') or '官方地址 ' + OFFICIAL[found['protocol']]}",
            f"密钥 留在 {SOURCES[source]} 的设置中，不写入 MossCode 的配置",
        ):
            self.write("  " + line)
        return provider if self._confirmed() else None

    def _preset(self, preset, *, existing_names: Collection[str] = (), default: bool = True) -> dict | None:
        from .catalog import lookup
        from .cli_providers import CLI_BINARIES, CLI_INSTALL_HINTS, CLI_PROTOCOLS

        if preset.protocol in CLI_PROTOCOLS:
            from .cli_providers import resolve_cli_executable

            binary = CLI_BINARIES[preset.protocol]
            self.write(f"  {preset.label}：{preset.protocol} · 本机官方 CLI（{binary}）")
            if found := resolve_cli_executable(binary):
                self.write(f"  已检测到本机命令：{found}（复用官方 CLI 登录态，不读取凭据）。")
            else:
                self.write(f"  提示：当前 PATH 绝对目录中未找到 {binary}；{CLI_INSTALL_HINTS[preset.protocol]}。")
        else:
            self.write(f"  {preset.label}：{preset.protocol} · {preset.base_url or OFFICIAL[preset.protocol]}")
        model = self.ask("模型名", preset.model)
        alias = preset.catalog_model if model == preset.model else None
        if lookup(model, base_url=preset.base_url, protocol=preset.protocol, alias=alias) is None:
            self.write(
                f"  模型目录中没有 {model}，按 128K 估算上下文；可以在配置中填写 context_window，"
                "或用 catalog_model 指定目录中的条目（moss models <关键词> 查找）。"
            )
        name = _unique_name(preset.key, existing_names)
        provider = {"name": name, "preset": preset.key, "model": model}
        if preset.auth_type == "cli":
            self.write("  复用本机官方 CLI 的登录状态（auth_type: cli），无需填写 API 密钥。")
        elif preset.auth_type == "none":
            self.write("  本地回环服务免 API 密钥（auth_type: none）。")
        elif preset.auth_type == "github-copilot":
            self.write("  提示：推荐使用 moss login copilot 完成 GitHub 设备码登录；此处可继续配置环境变量或凭据条目。")
            self._credential(provider, "凭据", preset.api_key_env)
        else:
            self._credential(provider, "凭据", preset.api_key_env)
        self.write("")
        self.write("将写入：")
        for line in summary(provider, default=default):
            self.write("  " + line)
        return provider if self._confirmed() else None

    def _confirmed(self) -> bool:
        return self.ask("确认写入？(Y/n)").lower() in {"", "y", "yes", "是"}

    def _custom(self, *, existing_names: Collection[str] = (), default: bool = True) -> dict | None:
        from .catalog import lookup, tokens_label
        from .config import catalog_window

        protocol = PROTOCOLS[self.choose("1/5 协议", [label for _, label in PROTOCOLS]) - 1][0]
        if protocol == "openai-chat-completions":
            self.write("  DeepSeek、Qwen 等兼容服务请填写它的地址，例如 https://api.deepseek.com")
        while True:
            base_url = self.ask(f"2/5 服务地址 base_url（回车使用官方地址 {OFFICIAL[protocol]}）")
            problem = _url_problem(base_url) if base_url else None
            if problem is None:
                break
            self.write("  " + problem)
        while not (model := self.ask("3/5 模型名（以服务文档为准）")):
            self.write("  模型名不能为空。")
        info = lookup(model, base_url=base_url or None, protocol=protocol)
        known = catalog_window(info) if info else None
        if known:
            # Not written to the file, so a later catalog update can correct it.
            hint = f"回车采用模型目录中的 {tokens_label(known)}"
        else:
            self.write(
                f"  模型目录中没有 {model}；可以在配置中填写 catalog_model 指定目录中的条目（moss models <关键词> 查找）。"
            )
            hint = "回车按 128K 估算"
        while True:
            answer = self.ask(f"4/5 上下文窗口 Token 数，如 200k（{hint}）")
            window = _window(answer) if answer else None
            if not answer or window is not None:
                break
            self.write("  请输入正整数，例如 128000、200k 或 1m。")
        host_base = _host_name(base_url)
        raw_name = host_base or NAMES[protocol]
        name = _unique_name(raw_name, existing_names)
        provider: dict = {"name": name, "protocol": protocol, "model": model}
        if base_url:
            provider["base_url"] = base_url
        env_default = f"{host_base.upper().replace('-', '_')}_API_KEY" if host_base else KEYS[protocol]
        self._credential(provider, "5/5 凭据", env_default)
        if window is not None:
            provider["context_window"] = window
        self.write("")
        self.write("将写入：")
        for line in summary(provider, known, default=default):
            self.write("  " + line)
        return provider if self._confirmed() else None

    def _credential(self, provider: dict, title: str, variable_default: str) -> None:
        """Where the key is kept: the credential manager (saved once confirmed), a variable, or the file."""
        name = provider["name"]
        self.write(title)
        problem = self.vault.problem()
        methods = (
            []
            if problem
            else [("keyring", f"系统凭据管理器（推荐）：密钥由 {self.vault.label()} 保存，配置文件只写名称")]
        )
        methods += [
            ("env", "环境变量" + ("（推荐）" if problem else "") + "：配置文件只写变量名，密钥放在环境变量中"),
            ("plain", "直接输入密钥：写入配置文件"),
        ]
        if problem:
            self.write(f"  {problem}，不能使用系统凭据管理器。")
        method = methods[self.choose("保存方式", [label for _, label in methods]) - 1][0]
        if method == "keyring":
            self.pending_key = self._key()
            provider["api_key_keyring"] = name
        elif method == "env":
            while not ENV_NAME.fullmatch(variable := self.ask("环境变量名", variable_default)):
                self.write("  请填写变量名（字母、数字和下划线），不要填写密钥本身。")
            provider["api_key_env"] = variable
        else:
            self.write("  " + PLAINTEXT_WARNING)
            provider["api_key"] = self._key()

    def _key(self) -> str:
        while True:
            try:
                key = self.secret("密钥（输入时不显示）：").strip()
            except EOFError:
                raise Cancelled from None
            if key:
                return key
            self.write("  密钥不能为空。")


def summary(provider: dict, catalog_window: int | None = None, *, default: bool = True) -> list[str]:
    """What will be written, the key itself never shown."""
    from .catalog import tokens_label

    window = f"未填写，按模型目录 {tokens_label(catalog_window)}" if catalog_window else "未填写，按 128K 估算"
    if "preset" in provider:
        from .catalog import lookup
        from .config import catalog_window as known
        from .presets import PRESETS, expand

        preset = PRESETS[provider["preset"]]
        provider = expand(provider)
        info = lookup(
            provider["model"],
            base_url=provider.get("base_url"),
            protocol=provider["protocol"],
            alias=provider.get("catalog_model"),
        )
        size = known(info) if info else None
        window = f"按模型目录 {tokens_label(size)}" if size else "按 128K 估算"
    from .cli_providers import CLI_BINARIES, CLI_PROTOCOLS

    role = "默认 Provider" if default else f"追加，可用 /model {provider['name']} 切换"
    if provider["protocol"] in CLI_PROTOCOLS:
        endpoint_line = f"服务地址 本机官方 CLI（{CLI_BINARIES[provider['protocol']]}）"
    else:
        endpoint_line = f"服务地址 {provider.get('base_url') or '官方地址 ' + OFFICIAL[provider['protocol']]}"
    lines = [
        f"名称 {provider['name']}（{role}）" + (f"，预设 {preset.label}" if "preset" in provider else ""),
        f"协议 {provider['protocol']}",
        f"模型 {provider['model']}",
        endpoint_line,
        f"上下文窗口 {provider.get('context_window') or window}",
    ]
    if provider.get("auth_type") == "cli" or provider["protocol"] in CLI_PROTOCOLS:
        lines.append("凭据 复用本机官方 CLI 登录态（不读取凭据）")
    elif provider.get("auth_type") == "none":
        lines.append("凭据 免密钥（仅本机回环地址）")
    elif "api_key_env" in provider:
        lines.append(f"凭据 环境变量 {provider['api_key_env']}")
    elif "api_key_keyring" in provider:
        lines.append(f"凭据 系统凭据管理器 {provider['api_key_keyring']}")
    else:
        lines.append("凭据 已设置（明文保存在配置文件中）")
    return lines


def write_config(path: Path, provider: dict) -> None:
    """Never replaces a file: the exclusive create fails if one appeared meanwhile."""
    from .config import render_config

    path.parent.mkdir(parents=True, exist_ok=True)
    write_temp_file(path, render_config(provider), private="api_key" in provider)


def _prepare_append(
    path: Path, original: str, existing_raw: dict, provider: dict, home: Path | None
) -> tuple[Path, bool]:
    """A validated sibling temp file holding the configuration with `provider` appended, and whether the original
    text (comments included) was kept. The caller replaces `path` with it, or deletes it."""
    import yaml

    from .config import load_settings

    expected = {**existing_raw, "providers": [*existing_raw["providers"], provider]}
    rendered = _insert_provider_text(original, provider)
    kept = False
    if rendered is not None:
        try:
            kept = yaml.safe_load(rendered) == expected
        except yaml.YAMLError:
            kept = False
    if not kept:
        rendered = (
            "# MossCode 配置，由 moss setup 生成。\n"
            "# 字段说明见 config.example.yaml；追加 Provider：运行 moss setup --add，或在 providers 下再写一项。\n"
            + yaml.safe_dump(expected, sort_keys=False, allow_unicode=True)
        )
    has_plaintext = any(isinstance(item, dict) and "api_key" in item for item in expected["providers"])
    tmp = path.with_name(f".config-{secrets.token_hex(4)}.yaml.tmp")
    try:
        write_temp_file(tmp, rendered, private=has_plaintext)
        load_settings(tmp, home)
    except BaseException:
        with suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise
    return tmp, kept


def setup(
    home: Path | None = None,
    wizard: Wizard | None = None,
    *,
    first_run: bool = True,
    add: bool = False,
) -> str:
    """One of ready, written (a credential is still to be set), exists, cancelled, failed; each is reported."""
    import yaml

    from .config import ConfigError, load_settings

    wizard = wizard or Wizard()
    path = user_config(home)
    existing_raw: dict | None = None
    existing_names: set[str] = set()
    original = ""
    if path.exists():
        if not add:
            wizard.write(
                f"配置已存在：{path}。setup 不会覆盖它；追加 Provider 请运行 moss setup --add，或直接编辑这个文件。"
            )
            return "exists"
        try:
            original = path.read_text(encoding="utf-8")
            loaded = yaml.safe_load(original)
        except (OSError, yaml.YAMLError, UnicodeError) as exc:
            wizard.write(f"无法读取现有配置 {path}：{exc}。请先修复后再运行 moss setup --add。")
            return "failed"
        if not isinstance(loaded, dict) or not isinstance(loaded.get("providers"), list) or not loaded["providers"]:
            wizard.write(f"现有配置 {path} 格式无效（缺少 providers 列表），无法追加。")
            return "failed"
        try:
            settings = load_settings(path, home)
        except ConfigError as exc:
            wizard.write(f"现有配置 {path} 校验失败：{exc} 请先修复后再运行 moss setup --add。")
            return "failed"
        existing_raw = loaded
        existing_names = {item.name for item in settings.providers}
        wizard.write(
            f"将在 {path} 中追加 Provider（已有：{'、'.join(item.name for item in settings.providers)}）。"
            "按 Ctrl+C 随时退出，不修改文件。"
        )
    else:
        if first_run:
            wizard.write("MossCode 还没有配置。下面创建用户级配置，之后在任何目录都能使用。")
        wizard.write(f"将写入 {path}。按 Ctrl+C 随时退出，不写入任何文件。")
    try:
        provider = wizard.collect(home, existing_names=existing_names, default=existing_raw is None)
    except (Cancelled, KeyboardInterrupt):
        wizard.write("\n已退出，没有写入文件。")
        return "cancelled"
    if provider is None:
        wizard.write("没有写入文件。")
        return "cancelled"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        wizard.write(f"无法创建配置目录 {path.parent}：{exc.strerror or exc}。")
        return "failed"
    # Lock the shared config transaction after user input completes, re-reading the latest file on disk so
    # concurrent TUI updates (e.g. Ctrl+D default changes or /connect) are never overwritten by a stale snapshot.
    with ExitStack() as stack:
        try:
            stack.enter_context(FileLock(str(config_lock_path(path)), timeout=CONFIG_LOCK_TIMEOUT))
        except (FileLockTimeout, OSError) as exc:
            wizard.write(f"配置文件正在被其他进程使用或无法加锁：{exc}。")
            return "failed"
        if path.exists():
            if not add:
                wizard.write(f"配置已存在：{path}。没有覆盖它。")
                return "exists"
            try:
                latest_original = path.read_text(encoding="utf-8")
                latest_raw = yaml.safe_load(latest_original)
                latest_settings = load_settings(path, home)
            except (OSError, yaml.YAMLError, UnicodeError, ConfigError) as exc:
                wizard.write(f"无法读取现有配置 {path}：{exc}。原有配置文件未修改。")
                return "failed"
            if (
                not isinstance(latest_raw, dict)
                or not isinstance(latest_raw.get("providers"), list)
                or not latest_raw["providers"]
            ):
                wizard.write(f"现有配置 {path} 格式无效（缺少 providers 列表），无法追加。")
                return "failed"
            latest_names = {item.name for item in latest_settings.providers}
            provider["name"] = _unique_name(provider["name"], latest_names)
            taken: set[str] = set()
            if "api_key_keyring" in provider:
                taken = latest_names | {
                    item["api_key_keyring"]
                    for item in latest_raw["providers"]
                    if isinstance(item, dict) and isinstance(item.get("api_key_keyring"), str)
                }
                provider["api_key_keyring"] = _unique_name(provider["name"], taken)
            try:
                prepared, kept = _prepare_append(path, latest_original, latest_raw, provider, home)
            except ConfigError as exc:
                wizard.write(f"追加 Provider 校验失败：{exc} 原有配置文件未修改。")
                return "failed"
            except OSError as exc:
                wizard.write(f"无法写入 {path}：{exc.strerror or exc}。")
                return "failed"
            stored_keyring: str | None = None
            if wizard.pending_key is not None:
                from .credentials import KeyringError

                try:
                    stack.enter_context(FileLock(str(keyring_lock_path(home)), timeout=CONFIG_LOCK_TIMEOUT))
                except (FileLockTimeout, OSError) as exc:
                    with suppress(OSError):
                        prepared.unlink(missing_ok=True)
                    wizard.write(f"凭据管理器正在被其他进程使用或无法加锁：{exc}。没有写入文件。")
                    return "failed"
                try:
                    allocated = allocate_keyring_name(provider["name"], taken, wizard.vault)
                except KeyringError as exc:
                    with suppress(OSError):
                        prepared.unlink(missing_ok=True)
                    wizard.write(f"无法读取系统凭据管理器：{exc}。没有写入文件。")
                    return "failed"
                if allocated != provider["api_key_keyring"]:
                    with suppress(OSError):
                        prepared.unlink(missing_ok=True)
                    provider["api_key_keyring"] = allocated
                    try:
                        prepared, kept = _prepare_append(path, latest_original, latest_raw, provider, home)
                    except (ConfigError, OSError) as exc:
                        wizard.write(f"追加 Provider 校验失败：{exc} 原有配置文件未修改。")
                        return "failed"
                try:
                    wizard.vault.store(provider["api_key_keyring"], wizard.pending_key)
                    stored_keyring = provider["api_key_keyring"]
                except KeyringError as exc:
                    with suppress(OSError):
                        prepared.unlink(missing_ok=True)
                    wizard.write(f"无法保存密钥：{exc}。没有写入文件。")
                    return "failed"
            backup = None
            try:
                if not kept:
                    backup = path.with_name(path.name + ".bak")
                    write_private_backup(path, backup)
                os.replace(prepared, path)
            except OSError as exc:
                if stored_keyring is not None:
                    rollback_keyring_entry(stored_keyring, wizard.vault)
                cleanup_err: OSError | None = None
                try:
                    prepared.unlink(missing_ok=True)
                except OSError as err:
                    cleanup_err = err
                wizard.write(f"无法写入 {path}：{exc.strerror or exc}。")
                if cleanup_err is not None:
                    wizard.write(f"另外，临时文件 {prepared} 清理失败：{cleanup_err.strerror or cleanup_err}。")
                return "failed"
            wizard.write(f"已向 {path} 追加 Provider {provider['name']}（可用 /model {provider['name']} 切换）。")
            if backup is not None:
                wizard.write(f"原文件的格式无法逐行保留，已重新生成；原文件（含注释）备份在 {backup}。")
        else:
            from .config import render_config

            def _stage_first() -> Path:
                candidate = path.with_name(f".config-{secrets.token_hex(4)}.yaml.tmp")
                try:
                    write_temp_file(candidate, render_config(provider), private="api_key" in provider)
                    load_settings(candidate, home)
                except BaseException:
                    with suppress(OSError):
                        candidate.unlink(missing_ok=True)
                    raise
                return candidate

            try:
                tmp = _stage_first()
            except ConfigError as exc:
                wizard.write(f"配置校验失败：{exc} 没有写入文件。")
                return "failed"
            except OSError as exc:
                wizard.write(f"无法写入 {path}：{exc.strerror or exc}。")
                return "failed"
            stored_keyring = None
            if wizard.pending_key is not None:
                from .credentials import KeyringError

                try:
                    stack.enter_context(FileLock(str(keyring_lock_path(home)), timeout=CONFIG_LOCK_TIMEOUT))
                except (FileLockTimeout, OSError) as exc:
                    with suppress(OSError):
                        tmp.unlink(missing_ok=True)
                    wizard.write(f"凭据管理器正在被其他进程使用或无法加锁：{exc}。没有写入文件。")
                    return "failed"
                try:
                    allocated = allocate_keyring_name(provider["api_key_keyring"], set(), wizard.vault)
                except KeyringError as exc:
                    with suppress(OSError):
                        tmp.unlink(missing_ok=True)
                    wizard.write(f"无法读取系统凭据管理器：{exc}。没有写入文件。")
                    return "failed"
                if allocated != provider["api_key_keyring"]:
                    with suppress(OSError):
                        tmp.unlink(missing_ok=True)
                    provider["api_key_keyring"] = allocated
                    try:
                        tmp = _stage_first()
                    except (ConfigError, OSError) as exc:
                        wizard.write(f"配置校验失败：{exc} 没有写入文件。")
                        return "failed"
                try:
                    wizard.vault.store(provider["api_key_keyring"], wizard.pending_key)
                    stored_keyring = provider["api_key_keyring"]
                except KeyringError as exc:
                    with suppress(OSError):
                        tmp.unlink(missing_ok=True)
                    wizard.write(f"无法保存密钥：{exc}。没有写入文件。")
                    return "failed"
            try:
                os.replace(tmp, path)
            except OSError as exc:
                if stored_keyring is not None:
                    rollback_keyring_entry(stored_keyring, wizard.vault)
                cleanup_err = None
                try:
                    tmp.unlink(missing_ok=True)
                except OSError as err:
                    cleanup_err = err
                wizard.write(f"无法写入 {path}：{exc.strerror or exc}。")
                if cleanup_err is not None:
                    wizard.write(f"另外，临时文件 {tmp} 清理失败：{cleanup_err.strerror or cleanup_err}。")
                return "failed"
            wizard.write(f"已写入 {path}。")
    variable = provider.get("api_key_env")
    if variable and not wizard.environ.get(variable):
        if os.name == "nt":
            wizard.write(f"环境变量 {variable} 还没有设置。在 PowerShell 中设置（只对新打开的终端生效）：")
            wizard.write(f'  [Environment]::SetEnvironmentVariable("{variable}", "你的密钥", "User")')
            wizard.write("然后打开新的终端，运行 moss。")
        else:
            wizard.write(f"环境变量 {variable} 还没有设置。在 Shell 配置（如 ~/.zshrc 或 ~/.bashrc）中添加：")
            wizard.write(f'  export {variable}="你的密钥"')
            wizard.write("重新加载终端配置后，运行 moss。")
        return "written"
    return "ready"


def run_setup(home: Path | None = None, wizard: Wizard | None = None) -> Path | None:
    """The first interactive run: the configuration to start with, or None to stop there."""
    return user_config(home) if setup(home, wizard) == "ready" else None


def main(argv: list[str] | None = None) -> int:
    command = argparse.ArgumentParser(
        prog="moss setup",
        description="创建用户级配置 ~/.mosscode/config.yaml：支持跟随 Claude Code / Codex、选择厂商预设或自定义配置。"
        "已有配置时不修改它；加 --add 可在已有配置中追加 Provider。",
    )
    command.add_argument("--add", action="store_true", help="在已有配置的 providers 中追加一项，保留原有设置")
    args = command.parse_args(argv)
    from .launcher import is_terminal

    if not (is_terminal(sys.stdin) and is_terminal(sys.stdout)):
        print("moss setup 需要在终端中运行。", file=sys.stderr)
        return 2
    result = setup(first_run=False, add=args.add)
    if result == "ready":
        print("运行 moss 开始使用。")
    return {"ready": 0, "written": 0, "exists": 0, "cancelled": 130, "failed": 1}[result]
