"""Inline TUI /connect onboarding wizard: presets, local server discovery, live probing, and hot-reload."""

from __future__ import annotations

import asyncio
import os
import webbrowser
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from rich.text import Text
from textual.widgets import Input, OptionList, Static

from .appearance import OPERATION_COLOR
from .builtin_cmds.common import choice_row
from .catalog import lookup, tokens_label
from .config import OFFICIAL, ConfigError, is_loopback_url, is_project_file, load_settings
from .credentials import keyring_label, keyring_problem, protect_providers, remember
from .follow import SOURCES as FOLLOWED
from .follow import detected as detected_follow
from .follow import resolve as resolve_follow
from .model_discovery import (
    DetectedLocalServer,
    ProbeResult,
    detect_local_servers,
    probe_provider,
    save_provider_entry,
)
from .presets import PRESETS
from .setup import ENV_NAME, NAMES, PLAINTEXT_WARNING, PROTOCOLS, _host_name, _url_problem

CONNECT_LIST_HELP = "↑ / ↓ 选择 · Enter 确认 · Esc 返回/关闭"
CONNECT_INPUT_HELP = "输入后按 Enter 确认 · Esc 返回上一步"


@dataclass
class _ConnectDraft:
    kind: str = ""  # "local" | "follow" | "preset" | "custom"
    key: str = ""  # preset key or follow source
    name: str = ""
    protocol: str = "openai-chat-completions"
    base_url: str | None = None
    model: str = ""
    auth_type: str = "api_key"
    cred_method: str = ""  # "keyring" | "env" | "plain" | "none"
    api_key_env: str = ""
    pending_key: str | None = None
    plain_key: str | None = None
    live_models: tuple[str, ...] = ()
    probe: ProbeResult | None = None
    default_env_name: str = "OPENAI_API_KEY"
    default_model: str = ""


class _ConnectMixin:
    """Interactive /connect provider wizard mixed into MossCodeApp."""

    def _connect_home(self) -> Path | None:
        settings = self.settings
        source = getattr(settings, "source", None) if settings is not None else None
        if (
            source is not None
            and not is_project_file(source)
            and source.name == "config.yaml"
            and source.parent.name == ".mosscode"
        ):
            return source.parent.parent
        return None

    def _connect_config_path(self) -> Path | None:
        settings = self.settings
        source = getattr(settings, "source", None) if settings is not None else None
        if source is not None and not is_project_file(source):
            return Path(source)
        return None

    def open_connect(self) -> None:
        """Open the inline /connect wizard."""
        if self._approval_request or self._question or self._generating:
            return
        if self.query("#model-panel") and self.query_one("#model-panel").display:
            self._close_model_panel()
        self._connect_step = "source"
        self._connect_history: list[str] = []
        self._connect_draft = _ConnectDraft()
        self._connect_committed: tuple[Path, str] | None = None
        self._connect_oauth_cancel: Any = None
        self._connect_local_servers: list[DetectedLocalServer] = []
        self._connect_choices: list[tuple[str, Any]] = []
        self._connect_status_text = Text("")
        self.query_one("#connect-panel").display = True
        self._place_prompt()
        self._render_connect_source()
        self.run_worker(self._discover_local_for_connect(), group="connect-local", exclusive=True)
        self.call_later(self._fit_chat)

    def _cancel_connect_oauth(self) -> None:
        cancel = getattr(self, "_connect_oauth_cancel", None)
        self._connect_oauth_cancel = None
        if callable(cancel):
            with suppress(Exception):
                cancel()
        with suppress(Exception):
            self.workers.cancel_group(self, "connect-oauth")

    def _close_connect_panel(self) -> None:
        self._cancel_connect_oauth()
        self._connect_step = ""
        self._connect_committed = None
        self._connect_choices = []
        self._connect_status_text = Text("")
        if self.query("#connect-panel"):
            self.query_one("#connect-panel").display = False
            self.query_one("#connect-input", Input).display = False
            self.query_one("#connect-options", OptionList).display = False
            self.query_one("#connect-status", Static).display = False
        self._place_prompt()
        if not self._approval_request and not self._question:
            self._focus_home()
        self._show_members()
        self.call_later(self._fit_chat)

    def _connect_back(self) -> None:
        if getattr(self, "_connect_step", "") == "oauth_waiting":
            self._cancel_connect_oauth()
        if getattr(self, "_connect_committed", None) is not None or not getattr(self, "_connect_history", None):
            self._close_connect_panel()
            return
        prev = self._connect_history.pop()
        self._connect_step = prev
        if prev == "source":
            self._render_connect_source()
        elif prev == "custom_protocol":
            self._render_connect_custom_protocol()
        elif prev == "base_url":
            self._render_connect_base_url()
        elif prev == "credential":
            self._render_connect_credential()
        elif prev == "secret":
            self._render_connect_secret()
        elif prev == "model":
            self._render_connect_model()
        elif prev == "custom_model":
            self._render_connect_custom_model()
        else:
            self._close_connect_panel()

    def _push_connect_step(self, next_step: str) -> None:
        if self._connect_step and self._connect_step not in {"probing", "oauth_waiting"}:
            self._connect_history.append(self._connect_step)
        self._connect_step = next_step

    def _show_connect_list(self, title: str, rows: list[Text], status: str = "") -> None:
        self.query_one("#connect-title", Static).update(title)
        self._connect_status_text = Text(status)
        st = self.query_one("#connect-status", Static)
        st.display = bool(status)
        if status:
            st.update(status)
        inp = self.query_one("#connect-input", Input)
        inp.display = False
        opts = self.query_one("#connect-options", OptionList)
        opts.clear_options()
        opts.add_options(rows)
        opts.styles.height = min(8, max(1, len(rows)))
        opts.highlighted = 0
        opts.display = True
        self.query_one("#connect-help", Static).update(CONNECT_LIST_HELP)
        opts.focus()
        self.call_later(self._fit_chat)

    def _show_connect_input(
        self,
        title: str,
        *,
        value: str = "",
        placeholder: str = "",
        password: bool = False,
        status: str = "",
    ) -> None:
        self.query_one("#connect-title", Static).update(title)
        self._connect_status_text = Text(status)
        st = self.query_one("#connect-status", Static)
        st.display = bool(status)
        if status:
            st.update(status)
        opts = self.query_one("#connect-options", OptionList)
        opts.display = False
        inp = self.query_one("#connect-input", Input)
        inp.password = password
        inp.placeholder = placeholder
        inp.value = value
        inp.display = True
        self.query_one("#connect-help", Static).update(CONNECT_INPUT_HELP)
        inp.focus()
        self.call_later(self._fit_chat)

    async def _discover_local_for_connect(self) -> None:
        detector = getattr(self, "_local_server_detector", detect_local_servers)
        try:
            found = await detector(timeout=0.5)
        except Exception:
            return
        if not found or getattr(self, "_connect_step", "") != "source":
            return
        self._connect_local_servers = list(found)
        self._render_connect_source()

    def _render_connect_source(self) -> None:
        choices: list[tuple[str, Any]] = []
        rows: list[Text] = []
        for srv in self._connect_local_servers:
            count_note = f"{len(srv.models)} 个模型" if srv.models else "已运行"
            choices.append(("local", srv))
            rows.append(
                choice_row(
                    f"{srv.label}（检测到本机运行中）",
                    f"{srv.base_url} · {count_note} · 免密钥",
                    muted_color=self.muted_color,
                )
            )
        for source in detected_follow(self._connect_home()):
            choices.append(("follow", source))
            rows.append(
                choice_row(
                    f"跟随 {FOLLOWED[source]} 的设置",
                    "复用已配置的端点与密钥",
                    muted_color=self.muted_color,
                )
            )
        choices.append(("oauth_openrouter", None))
        rows.append(
            choice_row(
                "OpenRouter（浏览器一键登录 · OAuth PKCE）",
                "浏览器授权并加密存入系统凭据管理器",
                muted_color=self.muted_color,
            )
        )
        choices.append(("oauth_copilot", None))
        rows.append(
            choice_row(
                "GitHub Copilot（浏览器设备码登录）",
                "使用 GitHub Copilot 订阅并自动刷新会话令牌",
                muted_color=self.muted_color,
            )
        )
        for key, preset in PRESETS.items():
            choices.append(("preset", key))
            detail = f"{preset.base_url or OFFICIAL[preset.protocol]} · 默认 {preset.model}"
            if preset.auth_type == "none":
                detail += " · 免密钥"
            elif preset.auth_type == "github-copilot":
                detail += " · 设备码令牌"
            rows.append(choice_row(preset.label, detail, muted_color=self.muted_color))
        choices.append(("custom", None))
        rows.append(
            choice_row(
                "自定义模型服务",
                "手动填写协议、服务地址与模型名",
                muted_color=self.muted_color,
            )
        )
        self._connect_choices = choices
        self._show_connect_list("连接模型服务 · 1. 选择服务商", rows)

    def _render_connect_custom_protocol(self) -> None:
        self._connect_choices = [("protocol", proto) for proto, _ in PROTOCOLS]
        rows = [choice_row(label, proto, muted_color=self.muted_color) for proto, label in PROTOCOLS]
        self._show_connect_list("连接模型服务 · 选择协议", rows)

    def _render_connect_base_url(self, error: str = "") -> None:
        draft = self._connect_draft
        default_url = draft.base_url or ""
        hint = f"留空则使用官方地址 {OFFICIAL[draft.protocol]}" if draft.kind == "custom" else "回车确认默认地址"
        self._show_connect_input(
            "连接模型服务 · 填写服务地址 (base_url)",
            value=default_url,
            placeholder=OFFICIAL[draft.protocol],
            status=error or hint,
        )

    def _render_connect_credential(self) -> None:
        draft = self._connect_draft
        problem = keyring_problem()
        choices: list[tuple[str, Any]] = []
        rows: list[Text] = []
        if problem is None:
            choices.append(("cred", "keyring"))
            rows.append(
                choice_row(
                    "系统凭据管理器（推荐）",
                    f"由 {keyring_label()} 加密保存，配置文件不存明文",
                    muted_color=self.muted_color,
                )
            )
        choices.append(("cred", "env"))
        rows.append(
            choice_row(
                "环境变量",
                f"配置文件只写变量名（默认 {draft.default_env_name}）",
                muted_color=self.muted_color,
            )
        )
        choices.append(("cred", "plain"))
        rows.append(
            choice_row(
                "直接写入配置文件",
                "明文保存在 ~/.mosscode/config.yaml",
                muted_color=self.muted_color,
            )
        )
        if is_loopback_url(draft.base_url):
            choices.append(("cred", "none"))
            rows.append(
                choice_row(
                    "免密钥（仅本机回环地址）",
                    "适用于 Ollama / LM Studio / 本地 vLLM",
                    muted_color=self.muted_color,
                )
            )
        self._connect_choices = choices
        self._show_connect_list(
            f"连接模型服务 · 2. 选择 {draft.name} 的凭据保存方式",
            rows,
            status=f"提示：{problem}" if problem else "",
        )

    def _render_connect_secret(self, error: str = "") -> None:
        draft = self._connect_draft
        if draft.cred_method == "env":
            self._show_connect_input(
                f"连接模型服务 · 输入环境变量名（{draft.name}）",
                value=draft.api_key_env or draft.default_env_name,
                placeholder=draft.default_env_name,
                password=False,
                status=error or "填写环境变量名称（如 DEEPSEEK_API_KEY），不要填写密钥本身。",
            )
        else:
            note = PLAINTEXT_WARNING if draft.cred_method == "plain" else "密钥将加密存入系统凭据管理器，输入时不回显。"
            self._show_connect_input(
                f"连接模型服务 · 输入 {draft.name} 的 API 密钥",
                value="",
                placeholder="粘贴 API Key 后按 Enter",
                password=True,
                status=error or note,
            )

    def _start_connect_oauth(self, provider_key: str) -> None:
        vault = getattr(self, "_connect_vault", None)
        problem = (
            vault.problem() if (vault is not None and callable(getattr(vault, "problem", None))) else keyring_problem()
        )
        if problem is not None:
            self._connect_status_text = Text(
                f"! 系统凭据管理器不可用（{problem}），默认拒绝明文保存 OAuth 凭据；请配置凭据管理器或运行 moss login {provider_key} --plaintext。"
            )
            st = self.query_one("#connect-status", Static)
            st.display = True
            st.update(self._connect_status_text)
            self.call_later(self._fit_chat)
            return

        self._push_connect_step("oauth_waiting")
        label = "OpenRouter" if provider_key == "openrouter" else "GitHub Copilot"
        self.query_one("#connect-title", Static).update(f"连接模型服务 · 正在等待 {label} 浏览器授权…")
        status_msg = f"正在初始化 {label} 浏览器登录…"
        self._connect_status_text = Text(status_msg)
        st = self.query_one("#connect-status", Static)
        st.display = True
        st.update(status_msg)
        self.query_one("#connect-input", Input).display = False
        self.query_one("#connect-options", OptionList).display = False
        self.query_one("#connect-help", Static).update("等待浏览器完成授权… 按 Esc 取消返回")
        self.call_later(self._fit_chat)
        self.run_worker(self._run_connect_oauth(provider_key), group="connect-oauth", exclusive=True)

    async def _run_connect_oauth(self, provider_key: str) -> None:
        from .auth import AuthError, GitHubCopilotAuth, OpenRouterPKCEAuth

        def _update_status(text: str) -> None:
            if getattr(self, "_connect_step", "") != "oauth_waiting":
                return
            self._connect_status_text = Text(text)
            st = self.query_one("#connect-status", Static)
            st.display = True
            st.update(text)
            self.call_later(self._fit_chat)

        open_browser = getattr(self, "_oauth_browser_opener", webbrowser.open)
        factory = getattr(self, "_oauth_provider_factory", None)
        if callable(factory):
            auth_provider = factory(provider_key)
        elif provider_key == "openrouter":
            auth_provider = OpenRouterPKCEAuth()
        else:
            auth_provider = GitHubCopilotAuth()

        try:
            result = await auth_provider.login(on_notice=_update_status, open_browser=open_browser, timeout=180.0)
        except asyncio.CancelledError:
            return
        except AuthError as exc:
            if getattr(self, "_connect_step", "") != "oauth_waiting":
                return
            self._connect_choices = [("back", None)]
            rows = [choice_row("返回选择其他服务", "", muted_color=self.muted_color)]
            self._connect_step = "oauth_failed"
            self._show_connect_list(f"连接模型服务 · {provider_key} 登录失败", rows, status=f"! {exc}")
            return

        if getattr(self, "_connect_step", "") != "oauth_waiting":
            return
        preset = PRESETS[result.preset]
        draft = self._connect_draft
        draft.kind = "preset"
        draft.key = preset.key
        draft.name = preset.key
        draft.protocol = preset.protocol
        draft.base_url = result.base_url or preset.base_url
        draft.default_model = result.default_model
        draft.auth_type = result.auth_type
        draft.cred_method = "keyring"
        draft.pending_key = result.credential
        self._start_connect_probe()

    def _start_connect_probe(self) -> None:
        draft = self._connect_draft
        self._connect_step = "probing"
        self.query_one("#connect-title", Static).update(f"连接模型服务 · 正在探测 {draft.name}…")
        status_msg = f"正在连接 {draft.base_url or OFFICIAL[draft.protocol]} 并拉取可用模型列表…"
        self._connect_status_text = Text(status_msg)
        st = self.query_one("#connect-status", Static)
        st.display = True
        st.update(status_msg)
        self.query_one("#connect-input", Input).display = False
        self.query_one("#connect-options", OptionList).display = False
        self.query_one("#connect-help", Static).update("探测中… 按 Esc 返回")
        self.call_later(self._fit_chat)
        self.run_worker(self._run_connect_probe(), group="connect-probe", exclusive=True)

    async def _run_connect_probe(self) -> None:
        draft = self._connect_draft
        prober = getattr(self, "_provider_prober", probe_provider)
        key = draft.pending_key or draft.plain_key or (os.environ.get(draft.api_key_env) if draft.api_key_env else None)
        if draft.auth_type != "none" and not key:
            # Env var not set in current shell yet: skip live probe cleanly and let user choose model.
            draft.probe = ProbeResult(
                True,
                models=draft.live_models,
                detail=f"当前环境未设置 {draft.api_key_env}，已跳过在线探测，请直接选择模型。",
            )
            if getattr(self, "_connect_step", "") == "probing":
                self._connect_step = "model"
                self._render_connect_model()
            return
        result = await prober(
            draft.protocol,
            draft.base_url,
            key,
            auth_type=draft.auth_type,
            timeout=8.0,
        )
        if getattr(self, "_connect_step", "") != "probing":
            return
        draft.probe = result
        if result.models:
            draft.live_models = result.models
        self._connect_step = "model"
        self._render_connect_model()

    def _render_connect_model(self) -> None:
        draft = self._connect_draft
        probe = draft.probe
        choices: list[tuple[str, Any]] = []
        rows: list[Text] = []
        if probe is not None and not probe.ok:
            choices.append(("probe_retry", None))
            rows.append(choice_row("重新探测连通性", "再次请求 /models", muted_color=self.muted_color))
            choices.append(("manual_model", None))
            rows.append(
                choice_row(
                    f"忽略探测，手动填写模型（默认 {draft.default_model or '自定义'}）",
                    "仍然保存配置",
                    muted_color=self.muted_color,
                )
            )
            choices.append(("back", None))
            rows.append(choice_row("返回修改地址或凭据", "", muted_color=self.muted_color))
            self._connect_choices = choices
            self._show_connect_list(
                f"连接模型服务 · {draft.name} 连通性探测失败",
                rows,
                status=f"! {probe.error}",
            )
            return

        models = list(draft.live_models)
        if models and draft.default_model in models:
            models.remove(draft.default_model)
            models.insert(0, draft.default_model)
        elif not models and draft.default_model:
            models.append(draft.default_model)
        for mid in models[:30]:
            info = lookup(mid, base_url=draft.base_url, protocol=draft.protocol)
            detail_parts = []
            if info and info.context_window:
                detail_parts.append(f"{tokens_label(info.context_window)} 窗口")
            if info and info.vision:
                detail_parts.append("图")
            if info and info.reasoning:
                detail_parts.append("思")
            if mid in draft.live_models:
                detail_parts.append("在线发现")
            choices.append(("pick_model", mid))
            rows.append(choice_row(mid, " · ".join(detail_parts), muted_color=self.muted_color))
        choices.append(("manual_model", None))
        rows.append(choice_row("手动输入其他模型名…", "适用于未列出的自定义模型 ID", muted_color=self.muted_color))
        self._connect_choices = choices
        status_text = f"✓ {probe.detail}" if probe and probe.detail else ""
        self._show_connect_list(f"连接模型服务 · 3. 选择 {draft.name} 的模型", rows, status=status_text)

    def _render_connect_custom_model(self, error: str = "") -> None:
        draft = self._connect_draft
        self._show_connect_input(
            f"连接模型服务 · 输入 {draft.name} 的模型名",
            value=draft.model or draft.default_model,
            placeholder="例如 deepseek-chat / gemini-2.5-flash / llama3.1",
            status=error or "按服务商文档填写准确模型 ID。",
        )

    def _render_connect_confirm(self) -> None:
        draft = self._connect_draft
        url_shown = draft.base_url or f"官方地址 {OFFICIAL[draft.protocol]}"
        if draft.kind == "follow":
            cred_shown = f"跟随 {FOLLOWED[draft.key]}"
        elif draft.cred_method == "none":
            cred_shown = "免密钥（本机回环）"
        elif draft.cred_method == "keyring":
            cred_shown = "系统凭据管理器（已输入，不回显）"
        elif draft.cred_method == "env":
            cred_shown = f"环境变量 {draft.api_key_env}"
        else:
            cred_shown = "明文写入配置文件（已输入，不回显）"
        summary_line = f"{draft.name} · {draft.protocol} / {draft.model} · {url_shown} · 凭据：{cred_shown}"
        self._connect_choices = [
            ("save", True),
            ("save", False),
            ("back", None),
        ]
        rows = [
            choice_row("保存并立即切换（同时设为默认模型）", "下次启动自动使用", muted_color=self.muted_color),
            choice_row("保存并仅在本次会话切换", "保留原有默认模型", muted_color=self.muted_color),
            choice_row("返回上一步", "", muted_color=self.muted_color),
        ]
        self._show_connect_list("连接模型服务 · 4. 确认保存", rows, status=summary_line)

    def _build_entry_from_draft(self) -> tuple[dict[str, Any], str | None]:
        draft = self._connect_draft
        if draft.kind == "follow":
            return {"name": draft.name, "follow": draft.key}, None
        entry: dict[str, Any] = {"name": draft.name}
        if draft.kind in {"preset", "local"} and draft.key in PRESETS:
            entry["preset"] = draft.key
            entry["model"] = draft.model
            preset = PRESETS[draft.key]
            if draft.base_url and draft.base_url != preset.base_url:
                entry["base_url"] = draft.base_url
            if draft.auth_type == "none":
                if preset.auth_type != "none":
                    entry["auth_type"] = "none"
            elif draft.cred_method == "keyring":
                entry["api_key_keyring"] = draft.name
            elif draft.cred_method == "env":
                entry["api_key_env"] = draft.api_key_env
            elif draft.cred_method == "plain" and draft.plain_key:
                entry["api_key"] = draft.plain_key
        else:
            entry["protocol"] = draft.protocol
            entry["model"] = draft.model
            if draft.base_url:
                entry["base_url"] = draft.base_url
            if draft.cred_method == "none":
                entry["auth_type"] = "none"
            elif draft.cred_method == "keyring":
                entry["api_key_keyring"] = draft.name
            elif draft.cred_method == "env":
                entry["api_key_env"] = draft.api_key_env
            elif draft.cred_method == "plain" and draft.plain_key:
                entry["api_key"] = draft.plain_key
        return entry, draft.pending_key

    async def _finish_connect_save(self, *, set_default: bool) -> None:
        home = self._connect_home()
        committed = getattr(self, "_connect_committed", None)
        if committed is None:
            entry, pending_key = self._build_entry_from_draft()
            config_path = self._connect_config_path()
            vault = getattr(self, "_connect_vault", None)
            current_source = getattr(self.settings, "source", None) if self.settings is not None else None
            extra_taken: set[str] | None = None
            if self.settings is not None and current_source is not None and is_project_file(current_source):
                # Avoid colliding with project providers when saving into user config during a project-config session.
                extra_taken = {item.name for item in self.settings.providers} | {
                    item.name for item in self._configured_providers
                }
            try:
                saved_path, saved_name = await asyncio.to_thread(
                    save_provider_entry,
                    home,
                    entry,
                    pending_key=pending_key,
                    set_default=set_default,
                    vault=vault,
                    config_path=config_path,
                    extra_taken_names=extra_taken,
                )
            except (ConfigError, OSError) as exc:
                # Commit did not happen: keep draft and confirmation options intact for retry or back.
                self._connect_status_text = Text(f"! 保存失败：{exc}")
                st = self.query_one("#connect-status", Static)
                st.display = True
                st.update(f"! 保存失败：{exc}")
                self.call_later(self._fit_chat)
                return
            self._connect_committed = (saved_path, saved_name)
            if pending_key:
                remember(pending_key)
            if entry.get("api_key"):
                remember(str(entry["api_key"]))
        else:
            saved_path, saved_name = committed

        try:

            def _reload_and_protect():
                loaded = load_settings(saved_path, home)
                protect_providers(loaded.providers)
                return loaded

            reloaded = await asyncio.to_thread(_reload_and_protect)
        except (ConfigError, OSError) as exc:
            # Config is already committed on disk: only offer retrying reload/switch, never re-appending.
            self._connect_step = "reload_failed"
            self._connect_choices = [
                ("retry_reload", None),
                ("close", None),
            ]
            rows = [
                choice_row(
                    f"重试加载并切换到 {saved_name}",
                    f"配置已写入 {saved_path}，不会重复追加",
                    muted_color=self.muted_color,
                ),
                choice_row("关闭向导", "保留已写入的配置", muted_color=self.muted_color),
            ]
            self._show_connect_list(
                f"连接模型服务 · {saved_name} 已保存，但刷新配置失败",
                rows,
                status=f"! 已写入 {saved_path}，但重新加载失败：{exc}",
            )
            return

        async with self._model_switch_lock:
            current_source = getattr(self.settings, "source", None) if self.settings is not None else None
            if self.settings is not None and current_source is not None and is_project_file(current_source):
                # Preserve all project providers; `saved_name` was allocated disjoint from them before commit.
                saved_entry = next((item for item in reloaded.providers if item.name == saved_name), None)
                if saved_entry is not None:
                    merged_active = (*self.settings.providers, saved_entry)
                    self.settings = replace(self.settings, providers=merged_active)
                    self._configured_providers = (*self._configured_providers, saved_entry)
                    self.conversation.provider_configs = merged_active
            else:
                self.settings = reloaded
                self._configured_providers = reloaded.providers
                self.conversation.provider_configs = reloaded.providers
        self._close_connect_panel()
        await self._local_note(f"└ 已保存 Provider {saved_name} 到 {saved_path}。")
        await self.switch_model(saved_name)

    async def _handle_connect_option(self, index: int) -> None:
        if not (0 <= index < len(self._connect_choices)):
            return
        action, payload = self._connect_choices[index]
        if action in {"local", "follow", "preset", "custom", "oauth_openrouter", "oauth_copilot"}:
            # Start a fresh draft on every source selection so abandoned credentials cannot bleed over.
            self._connect_draft = _ConnectDraft()
            self._connect_committed = None
        draft = self._connect_draft

        if action == "back":
            self._connect_back()
            return
        if action == "close":
            self._close_connect_panel()
            return
        if action == "retry_reload":
            await self._finish_connect_save(set_default=False)
            return
        if action == "oauth_openrouter":
            self._start_connect_oauth("openrouter")
            return
        if action == "oauth_copilot":
            self._start_connect_oauth("copilot")
            return
        if action == "local":
            srv: DetectedLocalServer = payload
            draft.kind = "local"
            draft.key = srv.preset_key
            draft.name = srv.preset_key
            draft.protocol = "openai-chat-completions"
            draft.base_url = srv.base_url
            draft.auth_type = "none"
            draft.cred_method = "none"
            draft.live_models = srv.models
            draft.default_model = srv.models[0] if srv.models else PRESETS[srv.preset_key].model
            draft.probe = ProbeResult(
                True,
                models=srv.models,
                detail=f"已检测到本机 {srv.label}（{len(srv.models)} 个模型）",
            )
            self._push_connect_step("model")
            self._render_connect_model()
            return
        if action == "follow":
            source: str = payload
            try:
                found = resolve_follow({"name": source, "follow": source}, self._connect_home())
            except ConfigError as exc:
                self._close_connect_panel()
                await self._local_note(f"! {exc}", OPERATION_COLOR)
                return
            draft.kind = "follow"
            draft.key = source
            draft.name = source
            draft.protocol = found["protocol"]
            draft.base_url = found.get("base_url")
            draft.model = found["model"]
            self._push_connect_step("confirm")
            self._render_connect_confirm()
            return
        if action == "preset":
            preset = PRESETS[payload]
            draft.kind = "preset"
            draft.key = preset.key
            draft.name = preset.key
            draft.protocol = preset.protocol
            draft.base_url = preset.base_url
            draft.default_model = preset.model
            draft.default_env_name = preset.api_key_env or "OPENAI_API_KEY"
            if preset.auth_type == "none":
                draft.auth_type = "none"
                draft.cred_method = "none"
                self._push_connect_step("base_url")
                self._render_connect_base_url()
            elif preset.auth_type == "github-copilot":
                self._start_connect_oauth("copilot")
            else:
                draft.auth_type = "api_key"
                self._push_connect_step("credential")
                self._render_connect_credential()
            return
        if action == "custom":
            draft.kind = "custom"
            self._push_connect_step("custom_protocol")
            self._render_connect_custom_protocol()
            return
        if action == "protocol":
            draft.protocol = payload
            draft.name = NAMES[payload]
            self._push_connect_step("base_url")
            self._render_connect_base_url()
            return
        if action == "cred":
            draft.cred_method = payload
            if payload == "none":
                draft.auth_type = "none"
                self._start_connect_probe()
            else:
                draft.auth_type = "api_key"
                self._push_connect_step("secret")
                self._render_connect_secret()
            return
        if action == "probe_retry":
            self._start_connect_probe()
            return
        if action == "pick_model":
            draft.model = payload
            self._push_connect_step("confirm")
            self._render_connect_confirm()
            return
        if action == "manual_model":
            self._push_connect_step("custom_model")
            self._render_connect_custom_model()
            return
        if action == "save":
            await self._finish_connect_save(set_default=bool(payload))

    async def _handle_connect_input(self, value: str) -> None:
        cleaned = value.strip()
        step = getattr(self, "_connect_step", "")
        draft = self._connect_draft

        if step == "base_url":
            if cleaned:
                problem = _url_problem(cleaned)
                if problem:
                    self._render_connect_base_url(error=f"! {problem}")
                    return
                draft.base_url = cleaned
            else:
                draft.base_url = None if draft.kind == "custom" else draft.base_url
            if draft.kind == "custom":
                host_base = _host_name(draft.base_url)
                draft.name = host_base or NAMES[draft.protocol]
                draft.default_env_name = (
                    f"{host_base.upper().replace('-', '_')}_API_KEY" if host_base else "OPENAI_API_KEY"
                )
            if draft.auth_type == "none":
                if not is_loopback_url(draft.base_url):
                    self._render_connect_base_url(
                        error="! 免密钥模式仅允许本机回环地址（localhost / 127.0.0.1 / ::1）。"
                    )
                    return
                self._start_connect_probe()
            else:
                self._push_connect_step("credential")
                self._render_connect_credential()
            return

        if step == "secret":
            if draft.cred_method == "env":
                if not cleaned or ENV_NAME.fullmatch(cleaned) is None:
                    self._render_connect_secret(error="! 环境变量名只能包含字母、数字和下划线，不能填写密钥本身。")
                    return
                draft.api_key_env = cleaned
                draft.pending_key = draft.plain_key = None
            else:
                if not cleaned:
                    self._render_connect_secret(error="! API 密钥不能为空。")
                    return
                if draft.cred_method == "keyring":
                    draft.pending_key = cleaned
                    draft.plain_key = None
                else:
                    draft.plain_key = cleaned
                    draft.pending_key = None
            self._start_connect_probe()
            return

        if step == "custom_model":
            if not cleaned:
                self._render_connect_custom_model(error="! 模型名不能为空。")
                return
            draft.model = cleaned
            self._push_connect_step("confirm")
            self._render_connect_confirm()
