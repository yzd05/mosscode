"""Prompt editing, image attachment paste, queued messages, and @mention/command hints."""

from __future__ import annotations

import asyncio
from pathlib import Path

from rich.table import Table
from rich.text import Text
from textual.css.query import NoMatches
from textual.widgets import OptionList, Static, TextArea

from .appearance import MUTED_COLOR, OPERATION_COLOR
from .clipboard import ClipboardError, is_image_file
from .commands import Hint
from .images import ImageError, load_image
from .mcp_surface import mcp_mention_candidates
from .mentions import FileIndex, partial_mention, quoted
from .tui_views import Attachment, PromptArea


class _PromptInputMixin:
    """Prompt input, attachment paste, queue, and completion-hint methods mixed into MossCodeApp."""

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id == "prompt":
            value = event.text_area.text
            # Esc suppresses this edit only, not a matching command typed later.
            if value != self._dismissed_prompt:
                self._dismissed_prompt = None
            self._refresh_hints(value)
            self._refresh_attachments()
            shell = value.lstrip().startswith("!")
            prefix = self.query_one("#input-prefix", Static)
            if prefix.has_class("shell") != shell:
                prefix.set_class(shell, "shell")
                prefix.update("!" if shell else "❯")

    def _workspace_root(self) -> Path:
        registry = getattr(self.conversation, "registry", None)
        return registry.permissions.root if registry is not None else Path.cwd()

    def _image_notes(self, text: str) -> list[str]:
        return self.query_one("#prompt", PromptArea).image_notes(text)

    def _refresh_attachments(self) -> None:
        field = self.query_one("#prompt", PromptArea)
        notes = field.image_notes(field.text)
        panel = self.query_one("#attachments", Static)
        panel.display = bool(notes)
        if notes:
            panel.update(Text("\n".join(notes) + "\n删除占位符即取消该图片", style=MUTED_COLOR))

    def _hint(self, text: str) -> None:
        self._hint_until = self._now() + 2
        self._status(text)

    def on_prompt_area_image_paste(self, event: PromptArea.ImagePaste) -> None:
        if not getattr(getattr(self.conversation.provider, "config", None), "accepts_images", True):
            # Ctrl+V still pastes text and dragged-in paths stay as text; only an explicit Alt+V or a drag says why.
            if event.otherwise is not None:
                event.otherwise()
            if event.otherwise is None or event.files is not None:
                self._hint(f"当前模型不支持图片（providers.{self.provider_name}.images）")
            return
        # The clipboard owner can stall; reading it must not hold up the interface.
        self.run_worker(self._attach_images(event), group="image-paste")

    async def _attach_images(self, event: PromptArea.ImagePaste) -> None:
        field = event.text_area
        started_typing = self._question_typing
        try:
            if event.files is None:
                self._hint("正在读取剪贴板…")
                content = await self._read_clipboard()
            else:
                content = event.files
            if isinstance(content, list):
                content = [path for path in content if await asyncio.to_thread(is_image_file, path)] or None
            if self._question_typing != started_typing:
                self._hint("输入模式已切换，请重新粘贴")
                return
            if content is None:
                if event.otherwise is not None:
                    event.otherwise()
                else:
                    self._hint("剪贴板中没有图片")
                return
            if isinstance(content, bytes):
                items = [Attachment(await asyncio.to_thread(load_image, content), "剪贴板")]
            else:
                items = [Attachment(await asyncio.to_thread(load_image, path), path.name) for path in content]
        except (ClipboardError, ImageError, OSError) as exc:
            self._hint(f"无法粘贴图片：{exc}")
            return
        if self._question_typing != started_typing:
            self._hint("输入模式已切换，请重新粘贴")
            return
        field.attach(items)
        self._hint(f"已添加 {len(items)} 张图片" if len(items) > 1 else "已添加图片")

    def _take_back_queued(self) -> tuple[str, ...]:
        """↑ in the prompt: pull queued messages back for editing without interrupting the task."""
        if not self._task_running:
            return ()
        queued = self.conversation.take_queued()
        if queued:
            self._refresh_queue()
        return queued

    def _refresh_queue(self) -> None:
        queue = self.query_one("#queue", Static)
        queued = self.conversation.queued
        queue.display = bool(queued)
        if queued:
            text = Text()
            for index, item in enumerate(queued[:4]):
                first = item.strip().splitlines()[0] if item.strip() else ""
                more = "…" if len(first) > 80 or "\n" in item.strip() else ""
                text.append(("\n" if index else "") + "↳ 排队：" + first[:80] + more, style=OPERATION_COLOR)
            if len(queued) > 4:
                text.append(f"\n↳ 另有 {len(queued) - 4} 条", style=OPERATION_COLOR)
            text.append("\n  将在本轮工具执行后发送给模型 · ↑ 取回编辑 · Esc 取消任务时放回输入框", style=MUTED_COLOR)
            queue.update(text)
        self._status(self._status_note)

    def _refresh_hints(self, value: str) -> None:
        self._hint_value = value
        hints = self.query_one("#command-hints", OptionList)
        help_line = self.query_one("#command-help", Static)
        previous = (
            self._hint_choices[hints.highlighted][0]
            if hints.highlighted is not None and hints.highlighted < len(self._hint_choices)
            else None
        )
        recalled = self.query_one("#prompt", PromptArea).recalled
        self._hint_choices = (
            [] if recalled or value == self._dismissed_prompt else self.commands.matches(self._command_context(), value)
        )
        self._mention_cursors = []
        if not self._hint_choices and not recalled and value != self._dismissed_prompt:
            self._hint_choices, self._mention_cursors = self._mention_hints(value)
        hints.clear_options()
        width = min(24, max(10, (self.size.width - 8) // 2))
        if self._mention_cursors:
            width = min(72, max(10, self.size.width - 16))  # Paths need the room more than the description.
        for hint in self._hint_choices:
            row = Table.grid(padding=(0, 2), expand=True)
            row.add_column(width=width, no_wrap=True, overflow="ellipsis")
            row.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
            row.add_row(Text(hint.label or hint.value), Text(hint.description, style=MUTED_COLOR))
            hints.add_option(row)
        hints.display = bool(self._hint_choices)
        help_line.display = hints.display
        if self._hint_choices:
            hints.highlighted = next((i for i, choice in enumerate(self._hint_choices) if choice[0] == previous), 0)
            help_line.update("↑ / ↓ 选择 · Tab 补全 · Enter 确认 · Esc 收起")
        self.call_later(self._fit_chat)

    def _mention_hints(self, value: str) -> tuple[list[Hint], list[int]]:
        """Files, directories and MCP resources for the @ mention being typed before the cursor."""
        field = self.query_one("#prompt", PromptArea)
        if "@" not in value or value != field.text or value.lstrip().startswith("!"):
            return [], []  # A ! command's @ is the shell's.
        cursor = field.document.get_index_from_location(field.cursor_location)
        before, after = value[:cursor], value[cursor:]
        typed = partial_mention(before)
        if typed is None:
            return [], []
        index = self._mention_index()
        start, query = typed
        if index is not None and query in index.paths and not query.endswith("/"):
            return [], []  # Already a whole file's path: nothing left to choose.
        if before[start + 1 : start + 2] == '"' and after[:1] == '"':
            after = after[1:]  # The closing quote of the mention being replaced.
        file_candidates = index.candidates(query) if index is not None else []
        permissions = self.conversation.registry.permissions if self.conversation.registry is not None else None
        mcp_candidates = mcp_mention_candidates(
            self.conversation.mcp_manager,
            query,
            permissions=permissions,
            limit=max(0, 12 - len(file_candidates)) if not query else 12,
        )
        if not file_candidates and not mcp_candidates:
            return [], []
        items: list[tuple[str, str, bool]] = []
        if ":" in query:
            items.extend(mcp_candidates)
            items.extend((p, "目录" if p.endswith("/") else "", p.endswith("/")) for p in file_candidates)
        else:
            items.extend((p, "目录" if p.endswith("/") else "", p.endswith("/")) for p in file_candidates)
            items.extend(mcp_candidates)
        hints, cursors = [], []
        for path, desc, keep_open in items[:12]:
            inserted = "@" + quoted(path) + ("" if keep_open or after[:1].isspace() else " ")
            hints.append(Hint(before[:start] + inserted + after, desc, path))
            # Inside a quoted directory or server prefix the cursor stays before the quote, so its entries come next.
            cursors.append(start + len(inserted) - (1 if keep_open and inserted.endswith('"') else 0))
        return hints, cursors

    def _mention_index(self) -> FileIndex | None:
        root = self._workspace_root()
        if self._file_index is None or self._file_index.root != root:
            self._file_index = FileIndex(root)
        index = self._file_index
        if index.stale and not self._indexing:
            self._indexing = True
            self.run_worker(lambda: self._load_index(index), thread=True, group="file-index")
        return index if index.loaded_at is not None else None

    def _load_index(self, index: FileIndex) -> None:
        try:
            index.load()
        finally:
            self._indexing = False
        try:
            self.call_from_thread(self._index_loaded)
        except RuntimeError:  # The app closed meanwhile.
            pass

    def _index_loaded(self) -> None:
        try:
            field = self.query_one("#prompt", PromptArea)
        except NoMatches:
            return
        if "@" in field.text:
            self._refresh_hints(field.text)

    def on_text_area_selection_changed(self, event: TextArea.SelectionChanged) -> None:
        # Moving the cursor onto or off a mention changes what the menu offers, without any edit.
        if event.text_area.id == "prompt" and "@" in event.text_area.text:
            self._refresh_hints(event.text_area.text)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in {"move_hint", "complete_hint"}:
            field = self.focused
            if not isinstance(field, PromptArea) or field.id != "prompt" or field.disabled:
                return False
            if self._hint_value != field.value:
                # TextArea.Changed arrives via the message queue; sync hints before Enter/Tab priority checks.
                if field.value != self._dismissed_prompt:
                    self._dismissed_prompt = None
                self._refresh_hints(field.value)
            if not self._hint_choices:
                # After Esc closed the menu, Tab still completes a unique command prefix.
                return action == "complete_hint" and parameters != (False,) and self._unique_hint() is not None
            if action == "complete_hint" and parameters == (False,):
                hints = self.query_one("#command-hints", OptionList)
                return hints.highlighted is not None and self._hint_choices[hints.highlighted][0] != field.value.strip()
        return super().check_action(action, parameters)

    def action_move_hint(self, direction: int) -> None:
        hints = self.query_one("#command-hints", OptionList)
        if direction < 0:
            hints.action_cursor_up()
        else:
            hints.action_cursor_down()

    def _unique_hint(self) -> Hint | None:
        value = self.query_one("#prompt", PromptArea).value
        if value != self._dismissed_prompt or " " in value.strip():
            return None
        matches = self.commands.matches(self._command_context(), value)
        return matches[0] if len(matches) == 1 and matches[0].value != value.strip() else None

    def action_complete_hint(self, tab: bool = True) -> None:
        hints = self.query_one("#command-hints", OptionList)
        if not self._hint_choices:
            unique = self._unique_hint() if tab else None
            if unique is None:
                return
            completion = unique.value
        elif hints.highlighted is None:
            return
        elif self._mention_cursors:
            completion, cursor = self._hint_choices[hints.highlighted].value, self._mention_cursors[hints.highlighted]
            field = self.query_one("#prompt", PromptArea)
            self._dismissed_prompt = None  # A directory goes on to its own entries.
            field.value = completion
            field.move_cursor(field.document.get_location_from_index(cursor))
            self._refresh_hints(completion)
            return
        else:
            completion = self._hint_choices[hints.highlighted].value
        # Tab continues into the argument candidates of commands that have them.
        if tab and self.commands.takes_completion(self._command_context(), completion):
            completion += " "
        self._dismissed_prompt = None if completion.endswith(" ") else completion
        field = self.query_one("#prompt", PromptArea)
        field.value = completion
        self._refresh_hints(completion)
