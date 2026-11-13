"""`moss setup`, and the first interactive run without a configuration.

Plain questions in the terminal (input and getpass), so it works in any console and before Textual loads.
"""

from __future__ import annotations

import argparse
import getpass
import ipaddress
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

PROTOCOLS = (
    ("anthropic-messages", "Anthropic Messages（Claude，或兼容 Messages 的服务）"),
    ("openai-responses", "OpenAI Responses"),
    ("openai-chat-completions", "OpenAI Chat Completions（DeepSeek、Qwen 等兼容服务）"),
)
NAMES = {"anthropic-messages": "claude", "openai-responses": "openai", "openai-chat-completions": "compat"}
OFFICIAL = {
    "anthropic-messages": "https://api.anthropic.com",
    "openai-responses": "https://api.openai.com/v1",
    "openai-chat-completions": "https://api.openai.com/v1",
}  # Where the SDK goes without base_url.
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
    parts = urlsplit(value)
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

    def collect(self, home: Path | None = None) -> dict | None:
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
            return self._follow(key, home)
        if kind == "preset":
            return self._preset(PRESETS[key])
        return self._custom()

    def _follow(self, source: str, home: Path | None) -> dict | None:
        from .follow import SOURCES, resolve, settings_path

        provider = {"name": source, "follow": source}
        found = resolve(provider, home, self.environ)
        self.write("")
        self.write("将写入：")
        for line in (
            f"名称 {source}（默认 Provider）",
            f"跟随 {SOURCES[source]} 的设置 {settings_path(source, home, self.environ)}",
            f"现在是 {found['protocol']} / {found['model']}，"
            f"服务地址 {found.get('base_url') or '官方地址 ' + OFFICIAL[found['protocol']]}",
            f"密钥 留在 {SOURCES[source]} 的设置中，不写入 MossCode 的配置",
        ):
            self.write("  " + line)
        return provider if self._confirmed() else None

    def _preset(self, preset) -> dict | None:
        from .catalog import lookup

        self.write(f"  {preset.label}：{preset.protocol} · {preset.base_url or OFFICIAL[preset.protocol]}")
        model = self.ask("模型名", preset.model)
        alias = preset.catalog_model if model == preset.model else None
        if lookup(model, base_url=preset.base_url, protocol=preset.protocol, alias=alias) is None:
            self.write(
                f"  模型目录中没有 {model}，按 128K 估算上下文；可以在配置中填写 context_window，"
                "或用 catalog_model 指定目录中的条目（moss models <关键词> 查找）。"
            )
        provider = {"name": preset.key, "preset": preset.key, "model": model}
        self._credential(provider, "凭据", preset.api_key_env)
        self.write("")
        self.write("将写入：")
        for line in summary(provider):
            self.write("  " + line)
        return provider if self._confirmed() else None

    def _confirmed(self) -> bool:
        return self.ask("确认写入？(Y/n)").lower() in {"", "y", "yes", "是"}

    def _custom(self) -> dict | None:
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
        name = _host_name(base_url) or NAMES[protocol]
        provider: dict = {"name": name, "protocol": protocol, "model": model}
        if base_url:
            provider["base_url"] = base_url
        default = f"{name.upper().replace('-', '_')}_API_KEY" if _host_name(base_url) else KEYS[protocol]
        self._credential(provider, "5/5 凭据", default)
        if window is not None:
            provider["context_window"] = window
        self.write("")
        self.write("将写入：")
        for line in summary(provider, known):
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


def summary(provider: dict, catalog_window: int | None = None) -> list[str]:
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
    lines = [
        f"名称 {provider['name']}（默认 Provider）" + (f"，预设 {preset.label}" if "preset" in provider else ""),
        f"协议 {provider['protocol']}",
        f"模型 {provider['model']}",
        f"服务地址 {provider.get('base_url') or '官方地址 ' + OFFICIAL[provider['protocol']]}",
        f"上下文窗口 {provider.get('context_window') or window}",
    ]
    if "api_key_env" in provider:
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
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(render_config(provider))
    if "api_key" in provider and os.name != "nt":
        path.chmod(0o600)


def setup(home: Path | None = None, wizard: Wizard | None = None, *, first_run: bool = True) -> str:
    """One of ready, written (a credential is still to be set), exists, cancelled, failed; each is reported."""
    from .config import ConfigError, load_settings

    wizard = wizard or Wizard()
    path = user_config(home)
    if path.exists():
        wizard.write(f"配置已存在：{path}。setup 不会覆盖它；追加 Provider 请直接编辑这个文件。")
        return "exists"
    if first_run:
        wizard.write("MossCode 还没有配置。下面创建用户级配置，之后在任何目录都能使用。")
    wizard.write(f"将写入 {path}。按 Ctrl+C 随时退出，不写入任何文件。")
    try:
        provider = wizard.collect(home)
    except (Cancelled, KeyboardInterrupt):
        wizard.write("\n已退出，没有写入文件。")
        return "cancelled"
    if provider is None:
        wizard.write("没有写入文件。")
        return "cancelled"
    if wizard.pending_key is not None:
        from .credentials import KeyringError

        try:
            wizard.vault.store(provider["api_key_keyring"], wizard.pending_key)
        except KeyringError as exc:
            wizard.write(f"无法保存密钥：{exc}。没有写入文件。")
            return "failed"
    try:
        write_config(path, provider)
    except FileExistsError:
        wizard.write(f"配置已存在：{path}。没有覆盖它。")
        return "exists"
    except OSError as exc:
        wizard.write(f"无法写入 {path}：{exc.strerror or exc}。")
        return "failed"
    try:
        load_settings(path, home)
    except ConfigError as exc:
        wizard.write(f"已写入 {path}，但校验失败：{exc} 请修改文件后再运行 moss。")
        return "failed"
    wizard.write(f"已写入 {path}。")
    variable = provider.get("api_key_env")
    if variable and not wizard.environ.get(variable):
        wizard.write(f"环境变量 {variable} 还没有设置。在 PowerShell 中设置（只对新打开的终端生效）：")
        wizard.write(f'  [Environment]::SetEnvironmentVariable("{variable}", "你的密钥", "User")')
        wizard.write("然后打开新的终端，运行 moss。")
        return "written"
    return "ready"


def run_setup(home: Path | None = None, wizard: Wizard | None = None) -> Path | None:
    """The first interactive run: the configuration to start with, or None to stop there."""
    return user_config(home) if setup(home, wizard) == "ready" else None


def main(argv: list[str] | None = None) -> int:
    command = argparse.ArgumentParser(
        prog="moss setup",
        description="创建用户级配置 ~/.mosscode/config.yaml：逐项询问协议、地址、模型、上下文窗口和凭据。"
        "已有配置时不修改它。",
    )
    command.parse_args(argv)
    from .launcher import is_terminal

    if not (is_terminal(sys.stdin) and is_terminal(sys.stdout)):
        print("moss setup 需要在终端中运行。", file=sys.stderr)
        return 2
    result = setup(first_run=False)
    if result == "ready":
        print("运行 moss 开始使用。")
    return {"ready": 0, "written": 0, "exists": 0, "cancelled": 130, "failed": 1}[result]
