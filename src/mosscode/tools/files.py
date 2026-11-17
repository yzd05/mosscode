import ast
import functools
import json
import os
import tempfile
import warnings
from bisect import bisect_left
from pathlib import Path

from pydantic import Field, model_validator

from ..credentials import placeholder_problem
from ..images import ImageError, image_ref, load_image, sniff, store_failure
from .base import IMAGES, Params, Tool, ToolFailure, file_io, image_context
from .diffing import unified_diff

READ_LIMIT = 2000
LINE_LIMIT = 2000  # Characters per displayed line.
READ_BUDGET = 56 * 1024  # Bytes per page, below the registry's 64 KiB result cap.
PREVIEW_LINES = 16  # Diff lines shown in the approval panel.


class ReadParams(Params):
    path: str
    offset: int = Field(default=1, ge=1)
    limit: int = Field(default=READ_LIMIT, ge=1, le=100000)


class WriteParams(Params):
    path: str
    content: str


class EditItem(Params):
    old_text: str = Field(min_length=1)
    new_text: str
    replace_all: bool = False


class EditParams(Params):
    path: str
    old_text: str = ""
    new_text: str = ""
    replace_all: bool = False
    edits: list[EditItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_mode(self) -> "EditParams":
        if self.edits:
            if self.old_text or self.new_text or self.replace_all:
                raise ValueError("使用 edits 批量编辑时，不要同时指定顶层的 old_text、new_text 或 replace_all。")
            return self
        if not self.old_text:
            raise ValueError("必须提供非空的 old_text，或非空的 edits 列表。")
        if "new_text" not in self.model_fields_set:
            raise ValueError("单处编辑必须提供 new_text。")
        return self


class FileState:
    """What the model last saw of each file: (mtime_ns, size) at read or own write."""

    def __init__(self):
        self._seen: dict[str, tuple[int, int]] = {}

    @staticmethod
    def stamp(path: Path) -> tuple[int, int]:
        info = path.stat()
        return info.st_mtime_ns, info.st_size

    @staticmethod
    def _key(path: Path) -> str:
        return os.path.normcase(str(path))

    def clear(self) -> None:
        self._seen.clear()

    def forget(self, path: str | Path) -> None:
        self._seen.pop(self._key(path), None)

    def copy(self) -> "FileState":
        """A fork starts from what its parent has read."""
        state = FileState()
        state._seen = dict(self._seen)
        return state

    def record(self, path: Path, stamp: tuple[int, int] | None = None) -> None:
        self._seen[self._key(path)] = stamp if stamp is not None else self.stamp(path)

    def check(self, path: Path) -> None:
        seen = self._seen.get(self._key(path))
        if seen is None:
            raise ToolFailure("not_read", "修改已有文件前需要先用 read_file 读取它；文件未修改。")
        if self.stamp(path) != seen:
            raise ToolFailure(
                "stale_read", "文件在上次读取后被修改（可能来自用户、命令或其他程序）；请重新读取后再修改。文件未修改。"
            )


def read_text(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as stream:
        text = stream.read()
    if "\0" in text:
        raise ToolFailure("binary_file", "目标包含二进制内容，不能作为文本文件操作。")
    return text


def atomic_write(path: Path, content: str | bytes) -> None:
    if path.is_dir():
        raise IsADirectoryError()
    path.parent.mkdir(parents=True, exist_ok=True)
    previous_mode = path.stat().st_mode if path.exists() else None
    descriptor, temporary = tempfile.mkstemp(prefix=".mosscode-", dir=path.parent)
    try:
        if isinstance(content, bytes):
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
        else:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                stream.write(content)
        if previous_mode is not None:
            os.chmod(temporary, previous_mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def line_count(text: str) -> int:
    return len(text.splitlines())


class FileTool(Tool):
    def __init__(self, root: Path, state: FileState | None = None):
        super().__init__(root)
        self.state = state if state is not None else FileState()
        # The main conversation's Checkpoints, shared by sub-agents in the same workspace; None tracks nothing.
        self.checkpoints = None

    def _before_write(self, path: Path) -> None:
        if self.checkpoints is not None:
            self.checkpoints.before_write(path)


class ReadFile(FileTool):
    read_only = True
    name = "read_file"
    description = (
        f"读取 UTF-8 文本文件。每行格式为“行号 + Tab + 原文”；offset 从 1 开始，limit 默认 {READ_LIMIT} 行。"
        f"超过 {LINE_LIMIT} 字符的行会截断，单页还有字节上限并会提示下一页的 offset。项目目录之外的路径需要用户确认。"
        "也能查看图片（PNG、JPEG、GIF、WebP、BMP）：图片附在结果中，offset 和 limit 不起作用。"
    )
    prompt_summary = "读取文本文件或查看图片，文本可按行分段查看"
    prompt_guidelines = (
        "使用 read_file 查看文本原文，优先于 shell 读取；修改或覆盖已有文件前必须先用 read_file 读取，文件被外部改动后需重新读取。",
        "read_file 输出中 Tab 之前的行号不是文件正文，不要把行号加入编辑参数。遇到截断时根据返回提示继续读取相关范围。",
    )
    params_model = ReadParams

    async def execute(self, params: ReadParams):
        accepts, store = image_context()

        def read():
            path = self.read_path(params.path)
            # Stamp before reading: a change during the read makes the record stale.
            stamp = FileState.stamp(path)
            with path.open("rb") as stream:
                head = stream.read(32)
            if sniff(head) is not None:
                return self._image(path, stamp, accepts, store)
            selected, more, size, long_lines, total = [], False, 0, 0, 0
            with path.open("r", encoding="utf-8", newline="") as stream:
                for number, line in enumerate(stream, 1):
                    total = number
                    if "\0" in line:
                        raise ToolFailure("binary_file", "目标包含二进制内容。")
                    if number < params.offset:
                        continue
                    if more:
                        continue  # Count remaining lines only.
                    if len(selected) >= params.limit:
                        more = True
                        continue
                    text = line.rstrip("\n").rstrip("\r")
                    if len(text) > LINE_LIMIT:
                        text = text[:LINE_LIMIT] + "…（本行过长，已截断）"
                        long_lines += 1
                    displayed = f"{number:>6}\t{text}"
                    encoded = len(displayed.encode("utf-8")) + 1
                    if size + encoded > READ_BUDGET:
                        more = True
                        continue
                    selected.append(displayed)
                    size += encoded
            self.state.record(path, stamp)
            next_offset = params.offset + len(selected)
            if not selected:
                output = "（文件为空）" if total == 0 else f"（offset={params.offset} 超出文件末尾，文件共 {total} 行）"
            else:
                output = "\n".join(selected)
                if more:
                    output += f"\n（共 {total} 行，还有内容，请从 offset={next_offset} 继续读取）"
            return output, {
                "path": str(path),
                "lines": len(selected),
                "total_lines": total,
                "truncated": more,
                "long_lines": long_lines,
                "next_offset": next_offset if more else None,
            }

        return await file_io(read)

    def _image(self, path: Path, stamp, accepts: bool, store):
        if not accepts:
            raise ToolFailure(
                "image_unsupported", "当前模型不支持图片输入，无法查看这张图片；切换到支持图片的模型后可以读取。"
            )
        try:
            image = load_image(path)
        except ImageError as exc:
            raise ToolFailure(exc.code, str(exc)) from None
        name = self.display(path)
        try:
            ref = image_ref(image, name, store)
        except OSError as exc:
            # Not the generic "not_found": the image was read, only keeping a copy of it failed.
            raise ToolFailure("image_store_failed", f"图片已读取，但无法保存到会话目录：{store_failure(exc)}") from None
        self.state.record(path, stamp)
        details = {
            "path": str(path),
            "image": {"name": ref.name, "media_type": ref.media_type, "width": ref.width, "height": ref.height},
            IMAGES: (ref,),
        }
        return f"{name} · 图片 · {image.describe()}", details


class WriteFile(FileTool):
    name = "write_file"
    description = "创建或覆盖 UTF-8 文本文件，自动创建父目录。content 是要写入的完整文件内容。覆盖已有文件前必须先用 read_file 读取。"
    prompt_summary = "创建文件或整体重写文件"
    prompt_guidelines = (
        "write_file 用于新建文件或确有必要的整体重写，优先于 shell 写入；局部修改优先使用可用的精确编辑工具。",
        "write_file 会覆盖已有内容；覆盖前先读取文件，保留需要的原有内容和用户无关改动。",
    )
    params_model = WriteParams

    def approval_preview(self, params: WriteParams) -> str:
        path = self.path(params.path)
        name = self.display(path)
        if path.exists():
            return unified_diff(read_text(path), params.content, name, limit=PREVIEW_LINES) or "内容与现有文件相同。"
        lines = params.content.splitlines()
        shown = [f"+{line}" for line in lines[:PREVIEW_LINES]]
        if len(lines) > PREVIEW_LINES:
            shown.append(f"…（共 {len(lines)} 行，仅显示前 {PREVIEW_LINES} 行）")
        return "\n".join([f"新建文件 {name}（{line_count(params.content)} 行）", *shown])

    async def execute(self, params: WriteParams):
        if problem := placeholder_problem(params.content):
            raise ToolFailure("secret_placeholder", problem)

        def write():
            path = self.path(params.path)
            existed, before = path.is_file(), None
            if existed:
                self.state.check(path)
                try:
                    before = read_text(path)
                except (UnicodeError, ToolFailure):
                    pass  # Overwriting a non-text file: no diff.
            self._before_write(path)
            atomic_write(path, params.content)
            self.state.record(path)
            name = self.display(path)
            details = {"path": str(path), "bytes": len(params.content.encode("utf-8")), "created": not existed}
            if not existed:
                return f"已创建 {name}（{line_count(params.content)} 行）。", details
            if before is None:
                return f"已覆盖 {name}（原文件不是 UTF-8 文本）。", details
            details["diff"] = unified_diff(before, params.content, name)
            return f"已覆盖 {name}：{line_count(before)} 行 → {line_count(params.content)} 行。", details

        return await file_io(write)


@functools.cache
def _bash_language():
    try:
        import tree_sitter
        import tree_sitter_bash
    except ImportError:
        return None
    return tree_sitter.Language(tree_sitter_bash.language())


def _first_tree_error_line(root) -> int:
    node = root
    while True:
        if node.is_error or node.is_missing:
            return node.start_point[0] + 1
        nxt = next((child for child in node.children if child.has_error), None)
        if nxt is None:
            return node.start_point[0] + 1
        node = nxt


def _syntax_diagnostic(path: Path, text: str, name: str) -> str | None:
    """Return a concise syntax diagnostic for edited source files, or None when valid or unsupported."""
    suffix = path.suffix.lower()
    if suffix == ".py":
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                ast.parse(text, filename=name)
        except SyntaxError as exc:
            return f"第 {exc.lineno or 1} 行：{exc.msg or 'invalid syntax'}"
        except ValueError as exc:
            return str(exc)
        return None
    if suffix == ".json":
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            return f"第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}"
        return None
    if suffix in {".sh", ".bash"}:
        language = _bash_language()
        if language is None:
            return None
        from tree_sitter import Parser

        root = Parser(language).parse(text.encode("utf-8")).root_node
        if root.has_error:
            return f"第 {_first_tree_error_line(root)} 行：Shell 语法错误"
    return None


def _split_lf_lines(text: str) -> list[tuple[int, int, int, str, bool]]:
    """Return (line_start, content_end, line_end, content, has_nl) for each '\\n'-delimited line."""
    if not text:
        return []
    parts = text.split("\n")
    lines: list[tuple[int, int, int, str, bool]] = []
    offset = 0
    last = len(parts) - 1
    for idx, content in enumerate(parts):
        if idx == last:
            if content:
                lines.append((offset, offset + len(content), offset + len(content), content, False))
            break
        content_end = offset + len(content)
        lines.append((offset, content_end, content_end + 1, content, True))
        offset = content_end + 1
    return lines


def _block_indent_delta(
    work_slice: list[tuple[int, int, int, str, bool]],
    old_lines: list[str],
) -> tuple[str, str] | None:
    """Constant leading whitespace delta ('+'|'-', prefix) across all non-empty lines, or None."""
    delta: tuple[str, str] | None = None
    for (_, _, _, w_content, _), o_content in zip(work_slice, old_lines):
        w_clean = w_content.rstrip(" \t")
        o_clean = o_content.rstrip(" \t")
        if not w_clean or not o_clean:
            if w_clean != o_clean:
                return None
            continue
        w_body = w_clean.lstrip(" \t")
        o_body = o_clean.lstrip(" \t")
        if w_body != o_body:
            return None
        w_indent = w_clean[: len(w_clean) - len(w_body)]
        o_indent = o_clean[: len(o_clean) - len(o_body)]
        if w_indent == o_indent:
            line_delta = ("", "")
        elif len(w_indent) > len(o_indent) and w_indent.endswith(o_indent):
            line_delta = ("+", w_indent[: len(w_indent) - len(o_indent)])
        elif len(o_indent) > len(w_indent) and o_indent.endswith(w_indent):
            line_delta = ("-", o_indent[: len(o_indent) - len(w_indent)])
        else:
            return None
        if delta is None:
            delta = line_delta
        elif delta != line_delta:
            return None
    if delta is None or delta == ("", ""):
        return None
    return delta


def _shift_indent(text: str, sign: str, prefix: str) -> str:
    shifted: list[str] = []
    for part in text.split("\n"):
        if not part.strip(" \t"):
            shifted.append(part)
        elif sign == "+":
            shifted.append(prefix + part)
        elif part.startswith(prefix):
            shifted.append(part[len(prefix) :])
        else:
            k = 0
            while k < len(prefix) and k < len(part) and part[k] == prefix[k]:
                k += 1
            shifted.append(part[k:])
    return "\n".join(shifted)


def _fuzzy_line_match(work: str, old: str, new: str, *, replace_all: bool) -> tuple[list[tuple[int, int]], str]:
    """Deterministic line-based fallback: first trailing whitespace, then uniform indentation offset."""
    if not old.strip():
        return [], new
    old_ends_nl = old.endswith("\n")
    old_lines = (old[:-1] if old_ends_nl else old).split("\n")
    m = len(old_lines)
    work_lines = _split_lf_lines(work)
    if len(work_lines) < m:
        return [], new

    old_stripped = [line.rstrip(" \t") for line in old_lines]
    spans: list[tuple[int, int]] = []
    i = 0
    limit = len(work_lines) - m
    while i <= limit:
        last_line = work_lines[i + m - 1]
        if (not old_ends_nl or last_line[4]) and all(
            work_lines[i + k][3].rstrip(" \t") == old_stripped[k] for k in range(m)
        ):
            spans.append((work_lines[i][0], last_line[2] if old_ends_nl else last_line[1]))
            i += m if replace_all else 1
        else:
            i += 1
    if spans or replace_all:
        return spans, new

    candidates: list[tuple[int, int, tuple[str, str]]] = []
    for idx in range(limit + 1):
        last_line = work_lines[idx + m - 1]
        if old_ends_nl and not last_line[4]:
            continue
        delta = _block_indent_delta(work_lines[idx : idx + m], old_lines)
        if delta is not None:
            candidates.append((work_lines[idx][0], last_line[2] if old_ends_nl else last_line[1], delta))
    if len(candidates) == 1:
        span_start, span_end, (sign, prefix) = candidates[0]
        return [(span_start, span_end)], _shift_indent(new, sign, prefix)
    return [(s, e) for s, e, _ in candidates], new


class EditFile(FileTool):
    name = "edit_file"
    description = (
        "将文件中的 old_text 精确替换为 new_text，或通过 edits 一次原子应用多处顺序修改。"
        "默认要求每处恰好匹配一次；replace_all=true 时替换全部匹配。任一处匹配为零次或不唯一时整次调用不修改文件。"
        "编辑前必须先用 read_file 读取。"
    )
    prompt_summary = "以唯一原文匹配精确替换片段"
    prompt_guidelines = (
        "局部修改优先 edit_file；同一文件的多处局部修改可用 edits 一次提交。编辑前读取相关原文，old_text 不包含读取输出的行号。",
        "edit_file 的 old_text 使用足以唯一定位的最小原文上下文，避免携带大段无关内容；new_text 保留周边缩进和代码风格。重命名等需要替换全部出现位置时使用 replace_all。",
        "edit_file 匹配为零时重新读取当前文件；匹配多次时补充邻近上下文。依据返回的错误调整参数，不猜测原文或原样重复失败调用。",
    )
    params_model = EditParams

    @staticmethod
    def _replace_one(text: str, item: EditItem) -> tuple[str, int, int, int, int]:
        """Apply a single EditItem in memory, preserving untouched line endings and dominant newline."""
        if item.old_text == item.new_text:
            raise ToolFailure("no_change", "old_text 与 new_text 相同，无需修改；文件未修改。")
        has_crlf = "\r\n" in text or "\r\n" in item.old_text
        crlf_count = text.count("\r\n")
        lf_count = text.count("\n") - crlf_count
        dominant_nl = "\r\n" if crlf_count > 0 and crlf_count >= lf_count else "\n"

        work = text.replace("\r\n", "\n") if "\r\n" in text else text
        old = item.old_text.replace("\r\n", "\n") if has_crlf else item.old_text
        new = item.new_text.replace("\r\n", "\n") if has_crlf else item.new_text

        spans: list[tuple[int, int]] = []
        start = 0
        step = len(old) if item.replace_all else 1  # Overlaps count when one match is required.
        while (position := work.find(old, start)) >= 0:
            spans.append((position, position + len(old)))
            start = position + step
        repl_work = new
        if not spans:
            spans, repl_work = _fuzzy_line_match(work, old, new, replace_all=item.replace_all)

        count = len(spans)
        if count == 0:
            raise ToolFailure(
                "match_count",
                "原文匹配 0 次；文件未修改。请重新读取文件确认当前内容，old_text 不要包含行号。",
                matches=0,
            )
        if count > 1 and not item.replace_all:
            raise ToolFailure(
                "match_count",
                f"原文匹配 {count} 次，需要恰好 1 次；文件未修改。"
                "补充邻近上下文使其唯一，或设置 replace_all=true 替换全部。",
                matches=count,
            )

        crlf_positions: list[int] = []
        if "\r\n" in text:
            pos = -1
            for part in text.split("\r\n")[:-1]:
                pos += len(part) + 1
                crlf_positions.append(pos)

        repl_orig = repl_work.replace("\n", "\r\n") if dominant_nl == "\r\n" else repl_work
        work_chunks: list[str] = []
        text_chunks: list[str] = []
        w_prev = 0
        t_prev = 0
        w_len = 0
        last_repl_start = 0
        for ws, we in spans:
            ts = ws + bisect_left(crlf_positions, ws) if crlf_positions else ws
            te = we + bisect_left(crlf_positions, we) if crlf_positions else we
            prefix_w = work[w_prev:ws]
            work_chunks.append(prefix_w)
            w_len += len(prefix_w)
            last_repl_start = w_len
            work_chunks.append(repl_work)
            w_len += len(repl_work)
            text_chunks.append(text[t_prev:ts])
            text_chunks.append(repl_orig)
            w_prev = we
            t_prev = te
        work_chunks.append(work[w_prev:])
        text_chunks.append(text[t_prev:])
        result_work = "".join(work_chunks)
        result_text = "".join(text_chunks)
        if result_text == text:
            raise ToolFailure("no_change", "old_text 与 new_text 相同，无需修改；文件未修改。")

        first = work[: spans[0][0]].count("\n") + 1
        last = result_work[:last_repl_start].count("\n") + 1 + repl_work.count("\n")
        old_last = work[: spans[-1][0]].count("\n") + 1 + work[spans[-1][0] : spans[-1][1]].count("\n")
        return result_text, count, first, last, old_last

    @staticmethod
    def _replace(text: str, params: EditParams) -> tuple[str, int, int, int]:
        """New text, match count and the changed line range; nothing is written."""
        items = params.edits or [
            EditItem(old_text=params.old_text, new_text=params.new_text, replace_all=params.replace_all)
        ]
        current = text
        total_count = 0
        ranges: list[tuple[int, int]] = []
        for item in items:
            prev_lines = current.replace("\r\n", "\n").count("\n")
            current, count, first, last, old_last = EditFile._replace_one(current, item)
            delta_lines = current.replace("\r\n", "\n").count("\n") - prev_lines
            total_count += count
            updated: list[tuple[int, int]] = []
            for r_start, r_end in ranges:
                if r_end < first:
                    updated.append((r_start, r_end))
                elif r_start > old_last:
                    updated.append((r_start + delta_lines, r_end + delta_lines))
                else:
                    first = min(first, r_start)
                    last = max(last, r_end + delta_lines)
            updated.append((first, last))
            ranges = updated
        if current == text:
            raise ToolFailure("no_change", "编辑前后文件内容相同，无需修改；文件未修改。")
        return current, total_count, min(r[0] for r in ranges), max(r[1] for r in ranges)

    def approval_preview(self, params: EditParams) -> str:
        path = self.path(params.path)
        text = read_text(path)
        result = self._replace(text, params)[0]
        return unified_diff(text, result, self.display(path), limit=PREVIEW_LINES)

    async def execute(self, params: EditParams):
        for candidate in (params.new_text, *(item.new_text for item in params.edits)):
            if problem := placeholder_problem(candidate):
                raise ToolFailure("secret_placeholder", problem)

        def edit():
            path = self.path(params.path)
            self.state.check(path)
            text = read_text(path)
            result, count, first, last = self._replace(text, params)
            self._before_write(path)
            atomic_write(path, result)
            self.state.record(path)
            name = self.display(path)
            where = f"第 {first} 行" if last <= first else f"第 {first}–{last} 行"
            message = f"已修改 {name}：替换 {count} 处，{where}。"
            details = {"path": str(path), "matches": count, "diff": unified_diff(text, result, name)}
            if diagnostic := _syntax_diagnostic(path, result, name):
                message += f"\n注意：修改后检测到语法错误（{diagnostic}），请检查并修复。"
                details["syntax_error"] = diagnostic
            return message, details

        return await file_io(edit)
