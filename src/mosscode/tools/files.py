import os
import tempfile
from pathlib import Path

from pydantic import Field

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


class EditParams(Params):
    path: str
    old_text: str = Field(min_length=1)
    new_text: str
    replace_all: bool = False


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


class EditFile(FileTool):
    name = "edit_file"
    description = (
        "将文件中的 old_text 精确替换为 new_text，空格和换行也参与匹配。默认要求恰好匹配一处；"
        "replace_all=true 时替换全部匹配。零次或不唯一的匹配不修改文件。编辑前必须先用 read_file 读取。"
    )
    prompt_summary = "以唯一原文匹配精确替换一个片段"
    prompt_guidelines = (
        "局部修改优先 edit_file；编辑前读取相关原文，old_text 不包含读取输出的行号。",
        "edit_file 的 old_text 使用足以唯一定位的最小原文上下文，避免携带大段无关内容；new_text 保留周边缩进和代码风格。重命名等需要替换全部出现位置时使用 replace_all。",
        "edit_file 匹配为零时重新读取当前文件；匹配多次时补充邻近上下文。依据返回的错误调整参数，不猜测原文或原样重复失败调用。",
    )
    params_model = EditParams

    @staticmethod
    def _replace(text: str, params: EditParams) -> tuple[str, int, int, int]:
        """New text, match count and the changed line range; nothing is written."""
        if params.old_text == params.new_text:
            raise ToolFailure("no_change", "old_text 与 new_text 相同，无需修改；文件未修改。")
        # The model reads CRLF files as LF lines; match in LF and write back CRLF.
        crlf = "\r\n" in text and text.count("\r\n") == text.count("\n")
        work, old, new = text, params.old_text, params.new_text
        if crlf:
            work, old, new = (value.replace("\r\n", "\n") for value in (text, old, new))
        positions, start = [], 0
        step = len(old) if params.replace_all else 1  # Overlaps count when one match is required.
        while (position := work.find(old, start)) >= 0:
            positions.append(position)
            start = position + step
        count = len(positions)
        if count == 0:
            raise ToolFailure(
                "match_count",
                "原文匹配 0 次；文件未修改。请重新读取文件确认当前内容，old_text 不要包含行号。",
                matches=0,
            )
        if count > 1 and not params.replace_all:
            raise ToolFailure(
                "match_count",
                f"原文匹配 {count} 次，需要恰好 1 次；文件未修改。"
                "补充邻近上下文使其唯一，或设置 replace_all=true 替换全部。",
                matches=count,
            )
        result = work.replace(old, new) if params.replace_all else work.replace(old, new, 1)
        # Line range of the change in the new text; the prefix before the first match is unchanged.
        first = work[: positions[0]].count("\n") + 1
        last_start = positions[-1] + (len(new) - len(old)) * (count - 1)
        last = result[:last_start].count("\n") + 1 + new.count("\n")
        if crlf:
            result = result.replace("\n", "\r\n")
        return result, count, first, last

    def approval_preview(self, params: EditParams) -> str:
        path = self.path(params.path)
        text = read_text(path)
        result = self._replace(text, params)[0]
        return unified_diff(text, result, self.display(path), limit=PREVIEW_LINES)

    async def execute(self, params: EditParams):
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
            return (
                f"已修改 {name}：替换 {count} 处，{where}。",
                {"path": str(path), "matches": count, "diff": unified_diff(text, result, name)},
            )

        return await file_io(edit)
