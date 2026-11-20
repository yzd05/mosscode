"""Unified searchable /model picker with favorites, recents, and one-key default persistence."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path

from filelock import Timeout as FileLockTimeout
from rich.text import Text
from textual.widgets import Input, OptionList, Static

from .appearance import MUTED_COLOR
from .builtin_cmds.common import choice_row
from .builtin_cmds.runtime import _sibling_models
from .catalog import tokens_label
from .config import ConfigError, ProviderConfig, Settings, is_project_file, load_settings
from .credentials import protect_providers
from .follow import SOURCES as FOLLOWED
from .model_discovery import ModelPrefs, ModelPrefsStore, fuzzy_score, model_key, set_default_model
from .runtime_factory import with_model

MODEL_PICKER_HELP = "↑ / ↓ 选择 · Enter 切换 · Ctrl+D 设为默认 · Ctrl+F 收藏 · Ctrl+N 添加服务 · Esc 收起"
DEFAULT_PANEL_HELP = "↑ / ↓ 选择 · Enter 确认 · Esc 收起"


@dataclass(frozen=True)
class ModelChoice:
    """One selectable provider/model row in the unified model picker."""

    provider_name: str
    model: str
    override: str | None  # None selects the provider's configured default model.
    display_name: str
    detail: str
    search_text: str
    is_current: bool
    is_default: bool
    is_favorite: bool
    recent_rank: int | None


class _ModelPickerMixin:
    """Searchable model picker methods mixed into MossCodeApp."""

    def _prefs_store(self) -> ModelPrefsStore:
        # Favorites and recents are always user-level (~/.mosscode/model_prefs.json), never written into projects.
        return ModelPrefsStore()

    def _load_model_prefs(self) -> ModelPrefs:
        store = self._prefs_store()
        return store.load() if store is not None else ModelPrefs()

    def _record_recent_model(self, provider_name: str, model: str) -> None:
        store = self._prefs_store()
        if store is None:
            return
        pending: deque[tuple[ModelPrefsStore, str, str]] | None = getattr(self, "_recent_model_queue", None)
        if pending is None:
            pending = deque()
            self._recent_model_queue = pending
        pending.append((store, provider_name, model))
        if not getattr(self, "_recent_model_draining", False):
            self._recent_model_draining = True
            self.run_worker(self._drain_recent_models(), group="model-recent")

    async def _drain_recent_models(self) -> None:
        pending: deque[tuple[ModelPrefsStore, str, str]] = getattr(self, "_recent_model_queue", deque())
        try:
            while pending:
                store, provider_name, model = pending.popleft()
                try:
                    await asyncio.to_thread(store.record_recent, provider_name, model)
                except (OSError, FileLockTimeout):
                    pass
        finally:
            self._recent_model_draining = False
            if pending:
                self._recent_model_draining = True
                self.run_worker(self._drain_recent_models(), group="model-recent")

    def _build_model_choices(self) -> list[ModelChoice]:
        providers: tuple[ProviderConfig, ...] = self._configured_providers or (
            self.settings.providers if self.settings is not None else ()
        )
        if not providers:
            return []
        prefs = self._load_model_prefs()
        fav_order = {key: idx for idx, key in enumerate(prefs.favorites)}
        recent_order = {key: idx for idx, key in enumerate(prefs.recent)}
        default_provider = self.settings.default_provider if self.settings is not None else None
        active_provider = self.provider_name
        active_model = self.model or (
            self.conversation.provider_config.model if self.conversation.provider_config is not None else ""
        )

        choices: list[ModelChoice] = []
        seen_keys: set[str] = set()
        expand_siblings = any(_sibling_models(item, item.model) for item in providers)

        for item in providers:
            shown_model = item.model
            key = model_key(item.name, shown_model)
            is_current = item.name == active_provider and (not active_model or shown_model == active_model)
            is_default = item.name == default_provider
            is_fav = key in fav_order
            recent_rank = recent_order.get(key)

            detail_parts = [f"{item.protocol} / {shown_model}"]
            if item.context_window:
                detail_parts.append(f"{tokens_label(item.context_window)} 窗口")
            if item.accepts_images:
                detail_parts.append("图")
            if item.follow:
                detail_parts.append(f"跟随 {FOLLOWED[item.follow]}")
            if is_default:
                detail_parts.append("默认")
            if recent_rank is not None and not is_current:
                detail_parts.append("最近")

            label = item.name if not expand_siblings else f"{item.name}/{shown_model}"
            choices.append(
                ModelChoice(
                    provider_name=item.name,
                    model=shown_model,
                    override=None,
                    display_name=label,
                    detail=" · ".join(detail_parts),
                    search_text=f"{item.name} {shown_model} {item.protocol} {item.preset or ''}",
                    is_current=is_current,
                    is_default=is_default,
                    is_favorite=is_fav,
                    recent_rank=recent_rank,
                )
            )
            seen_keys.add(key)

            if item.name == active_provider and active_model and active_model != item.model:
                override_key = model_key(item.name, active_model)
                if override_key not in seen_keys:
                    seen_keys.add(override_key)
                    override_parts = [f"{item.protocol} / {active_model}", "当前覆盖"]
                    choices.append(
                        ModelChoice(
                            provider_name=item.name,
                            model=active_model,
                            override=active_model,
                            display_name=f"{item.name}/{active_model}",
                            detail=" · ".join(override_parts),
                            search_text=f"{item.name} {active_model} {item.protocol} {item.preset or ''}",
                            is_current=True,
                            is_default=False,
                            is_favorite=override_key in fav_order,
                            recent_rank=recent_order.get(override_key),
                        )
                    )

            if expand_siblings:
                siblings = _sibling_models(item, active_model if item.name == active_provider else item.model)
                for mid, info in siblings:
                    sib_key = model_key(item.name, mid)
                    if sib_key in seen_keys:
                        continue
                    seen_keys.add(sib_key)
                    sib_current = item.name == active_provider and mid == active_model
                    sib_parts = []
                    if info.context_window:
                        sib_parts.append(f"{tokens_label(info.context_window)} 窗口")
                    if info.max_output_tokens:
                        sib_parts.append(f"输出 {tokens_label(info.max_output_tokens)}")
                    if info.vision:
                        sib_parts.append("图")
                    if info.reasoning:
                        sib_parts.append("思")
                    if sib_key in recent_order and not sib_current:
                        sib_parts.append("最近")
                    choices.append(
                        ModelChoice(
                            provider_name=item.name,
                            model=mid,
                            override=mid,
                            display_name=f"{item.name}/{mid}",
                            detail=" · ".join(sib_parts) or f"{item.protocol} / {mid}",
                            search_text=f"{item.name} {mid} {item.protocol} {item.preset or ''}",
                            is_current=sib_current,
                            is_default=False,
                            is_favorite=sib_key in fav_order,
                            recent_rank=recent_order.get(sib_key),
                        )
                    )

        def _sort_key(c: ModelChoice):
            key = model_key(c.provider_name, c.model)
            if c.is_favorite:
                return (0, fav_order.get(key, 999), c.display_name)
            if c.recent_rank is not None:
                return (1, c.recent_rank, c.display_name)
            return (2, 0, "")

        # Keep configured provider order when neither favorites nor recents exist.
        if prefs.favorites or prefs.recent:
            choices.sort(key=_sort_key)
        return choices

    def _format_choice_row(self, choice: ModelChoice) -> Text:
        prefix = "★ " if choice.is_favorite else ""
        suffix = "  ✓" if choice.is_current else ""
        return choice_row(f"{prefix}{choice.display_name}{suffix}", choice.detail, muted_color=self.muted_color)

    def open_model_picker(self) -> None:
        """Open the unified searchable /model picker inside #model-panel."""
        if self._approval_request or self._question or self._generating:
            return
        if self.query("#connect-panel") and self.query_one("#connect-panel").display:
            return
        self._model_picker_choices = self._build_model_choices()
        if not self._model_picker_choices:
            self.open_connect()
            return
        self._model_picker_active = True
        self._pick = None
        self._on_cancel = None
        search = self.query_one("#model-filter", Input)
        search.value = ""
        search.display = True
        self.query_one("#model-help", Static).update(MODEL_PICKER_HELP)
        self._render_model_picker("")
        self._show_panel("切换模型 · 对话历史保留（输入关键词筛选）", len(self._model_picker_filtered))
        search.focus()

    def _render_model_picker(self, query: str) -> None:
        all_choices: list[ModelChoice] = getattr(self, "_model_picker_choices", [])
        if query.strip():
            scored = []
            for idx, choice in enumerate(all_choices):
                score = fuzzy_score(query, choice.search_text)
                if score is not None:
                    scored.append((score, -idx, choice))
            scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
            filtered = [item[2] for item in scored]
        else:
            filtered = list(all_choices)
        self._model_picker_filtered = filtered
        picker = self.query_one("#models", OptionList)
        picker.clear_options()
        if filtered:
            picker.add_options([self._format_choice_row(c) for c in filtered])
            current_idx = next((i for i, c in enumerate(filtered) if c.is_current), 0)
            picker.highlighted = current_idx
        else:
            picker.add_options([Text("未找到匹配的模型（可按 Ctrl+N 添加新服务）", style=MUTED_COLOR)])
            picker.highlighted = 0
        self._panel_rows = max(1, len(filtered))
        picker.styles.height = min(8, self._panel_rows)
        self.call_later(self._fit_chat)

    def _current_picker_choice(self) -> ModelChoice | None:
        if not getattr(self, "_model_picker_active", False):
            return None
        filtered: list[ModelChoice] = getattr(self, "_model_picker_filtered", [])
        if not filtered:
            return None
        idx = self.query_one("#models", OptionList).highlighted
        if idx is None or not (0 <= idx < len(filtered)):
            return None
        return filtered[idx]

    def _merge_reloaded_providers(
        self,
        reloaded: Settings,
        *,
        persisted_provider: str,
    ) -> tuple[ProviderConfig, ...]:
        """Use `reloaded.providers` as the base so newly appended providers are selectable,
        preserving intentional session model overrides for providers that still exist.
        """
        old_configured = {item.name: item for item in self._configured_providers}
        old_active = {item.name: item for item in (self.settings.providers if self.settings is not None else ())}
        merged: list[ProviderConfig] = []
        for item in reloaded.providers:
            prev_active = old_active.get(item.name)
            prev_configured = old_configured.get(item.name)
            if (
                item.name != persisted_provider
                and prev_active is not None
                and prev_configured is not None
                and prev_active.model != prev_configured.model
            ):
                try:
                    merged.append(with_model(item, prev_active.model))
                    continue
                except ConfigError:
                    pass
            merged.append(item)
        return tuple(merged)

    async def _apply_model_choice(self, choice: ModelChoice, *, persist_default: bool = False) -> None:
        self._close_model_panel()
        async with self._model_switch_lock:
            if choice.override is None:
                switched = await self._switch_model_unlocked(choice.provider_name)
            else:
                switched = await self._switch_model_unlocked(choice.provider_name, choice.override)
            if not switched or not persist_default:
                return
            source = getattr(self.settings, "source", None) if self.settings is not None else None
            if source is None:
                await self._local_note("└ 当前配置无关联文件，仅在本次会话生效。")
                return
            if is_project_file(source):
                await self._local_note(f"└ 当前使用的是项目配置 {source}，为避免改动仓库文件，未写入默认模型。")
                return
            configured = next(
                (item for item in self._configured_providers if item.name == choice.provider_name),
                None,
            )
            if configured is not None and configured.follow and choice.override is not None:
                await self._local_note(
                    f"└ {choice.provider_name} 跟随 {FOLLOWED[configured.follow]}，模型覆盖 {choice.override} 仅在本次会话生效，未写入默认配置；如需将该提供商设为默认，请对其主条目按 Ctrl+D。"
                )
                return
            ok, message = await asyncio.to_thread(
                set_default_model, Path(source), choice.provider_name, choice.override
            )
            if ok:
                try:

                    def _reload_and_protect() -> Settings:
                        loaded = load_settings(source)
                        protect_providers(loaded.providers)
                        return loaded

                    reloaded = await asyncio.to_thread(_reload_and_protect)
                    merged_providers = self._merge_reloaded_providers(
                        reloaded,
                        persisted_provider=choice.provider_name,
                    )
                    self.settings = replace(reloaded, providers=merged_providers)
                    self._configured_providers = reloaded.providers
                    self.conversation.provider_configs = merged_providers
                    installed = getattr(self, "_installed_provider_config", None) or self.conversation.provider_config
                    reloaded_active = next((item for item in merged_providers if item.name == self.provider_name), None)
                    if (
                        reloaded_active is not None
                        and choice.provider_name == self.provider_name
                        and installed is not None
                        and reloaded_active != installed
                    ):
                        await self._switch_model(reloaded_active, force=True)
                except Exception:
                    pass
            await self._local_note(("└ " if ok else "! ") + message)

    def action_open_models(self) -> None:
        """Alt+M shortcut to open the unified searchable model picker."""
        if self._approval_request or self._question or self._generating:
            return
        if self.query("#connect-panel") and self.query_one("#connect-panel").display:
            return
        if self.query_one("#model-panel").display:
            if getattr(self, "_model_picker_active", False):
                self._close_model_panel()
            return
        self.open_model_picker()

    def action_model_set_default(self) -> None:
        """Ctrl+D inside the model picker: switch and persist as global default."""
        choice = self._current_picker_choice()
        if choice is None:
            return
        self.run_worker(self._apply_model_choice(choice, persist_default=True), group="model-switch")

    def action_model_toggle_favorite(self) -> None:
        """Ctrl+F inside the model picker: toggle ★ favorite pinning."""
        choice = self._current_picker_choice()
        if choice is None:
            return
        store = self._prefs_store()
        if store is None:
            self._hint("当前会话未关联用户配置文件，无法保存收藏")
            return
        self.run_worker(self._toggle_favorite_async(store, choice), group="model-prefs", exclusive=True)

    async def _toggle_favorite_async(self, store: ModelPrefsStore, choice: ModelChoice) -> None:
        try:
            added = await asyncio.to_thread(store.toggle_favorite, choice.provider_name, choice.model)
        except (OSError, FileLockTimeout) as exc:
            self._hint(f"保存收藏失败：{exc}")
            return
        if not getattr(self, "_model_picker_active", False):
            return
        self._model_picker_choices = self._build_model_choices()
        query = self.query_one("#model-filter", Input).value
        self._render_model_picker(query)
        self._hint(
            f"已收藏 ★ {choice.provider_name}/{choice.model}"
            if added
            else f"已取消收藏 {choice.provider_name}/{choice.model}"
        )

    def action_model_open_connect(self) -> None:
        """Ctrl+N inside the model picker: jump straight to /connect wizard."""
        if not getattr(self, "_model_picker_active", False):
            return
        self._close_model_panel()
        self.open_connect()
