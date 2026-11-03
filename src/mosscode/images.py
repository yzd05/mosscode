"""Images in messages: recognizing, normalizing and referencing them. Pillow is imported only here."""

import base64
import hashlib
import io
import math
import os
import secrets
import warnings
from dataclasses import dataclass, field, replace
from pathlib import Path

MAX_SIDE = 2000
# Base64 size limit, below Anthropic's 5 MB per image.
MAX_ENCODED = 4_718_592
MAX_FILE = 50 * 1024 * 1024
JPEG_QUALITIES = (80, 70, 60, 50)
SHRINK_ROUNDS = 3
TOKEN_CAP = 1600
SENDABLE = {"image/png", "image/jpeg", "image/gif", "image/webp"}
EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}
ORIENTATION = 0x0112
# 128 bits of SHA-256 is unique enough and keeps paths under Windows' 260-character limit.
NAME_DIGITS = 32


class ImageError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def sniff(head: bytes) -> str | None:
    """The image type from the file header; the extension is never trusted."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"BM") and len(head) >= 26:
        return "image/bmp"
    return None


def encoded_size(length: int) -> int:
    return 4 * math.ceil(length / 3)


def estimate_image_tokens(width: int, height: int) -> int:
    """Anthropic's width × height / 750, capped where it scales large images down; errs high elsewhere."""
    return min(math.ceil(width * height / 750), TOKEN_CAP)


def size_label(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MB"
    return f"{max(size, 1) / 1024:.0f} KB" if size >= 1024 else f"{size} B"


@dataclass(frozen=True)
class ProcessedImage:
    data: bytes
    media_type: str
    width: int
    height: int
    original_width: int
    original_height: int
    original_size: int

    @property
    def resized(self) -> bool:
        return (self.width, self.height) != (self.original_width, self.original_height)

    @property
    def name(self) -> str:
        return f"{hashlib.sha256(self.data).hexdigest()[:NAME_DIGITS]}.{EXTENSIONS[self.media_type]}"

    def describe(self) -> str:
        """`3840×2160 · 2.1 MB（发送尺寸 2000×1125）`"""
        text = f"{self.original_width}×{self.original_height} · {size_label(self.original_size)}"
        return text + (f"（发送尺寸 {self.width}×{self.height}）" if self.resized else "")


@dataclass(frozen=True)
class ImageRef:
    """An image in a message. Transcripts record the fields; the bytes live in a file or, without one, here."""

    name: str  # <sha256 prefix>.<ext> in the session's images directory.
    media_type: str
    width: int
    height: int
    size: int
    alt: str  # What stands in for the image as text: "[图片 #1]" or a file path.
    path: Path | None = field(default=None, compare=False, repr=False)
    data: bytes | None = field(default=None, compare=False, repr=False)

    def read(self) -> bytes:
        if self.data is not None:
            return self.data
        if self.path is None:
            raise FileNotFoundError(self.name)
        return self.path.read_bytes()

    def base64(self) -> str:
        return base64.b64encode(self.read()).decode("ascii")

    @property
    def tokens(self) -> int:
        return estimate_image_tokens(self.width, self.height)

    def placeholder(self, note: str) -> str:
        """The text sent instead of the image."""
        return f"{self.alt}（{note}）"


def store_image(directory: Path, image: ProcessedImage, alt: str) -> ImageRef:
    """directory/<sha256 prefix>.<ext>: the same image is stored once."""
    path = directory / image.name
    if not path.is_file():
        directory.mkdir(parents=True, exist_ok=True)
        partial = directory / f".{secrets.token_hex(4)}.tmp"
        partial.write_bytes(image.data)
        os.replace(partial, path)
    return ImageRef(image.name, image.media_type, image.width, image.height, len(image.data), alt, path=path)


def image_ref(image: ProcessedImage, alt: str, store=None) -> ImageRef:
    """Kept by store (anything with store_image) when given, otherwise in memory."""
    if store is not None:
        return store.store_image(image, alt)
    return ImageRef(image.name, image.media_type, image.width, image.height, len(image.data), alt, data=image.data)


def with_alt(ref: ImageRef, alt: str) -> ImageRef:
    return replace(ref, alt=alt)


def load_image(source: Path | bytes) -> ProcessedImage:
    """Turned upright, at most MAX_SIDE pixels a side and MAX_ENCODED in base64; unchanged when already so."""
    if isinstance(source, Path):
        if source.stat().st_size > MAX_FILE:
            raise ImageError("image_too_large", f"图片超过 {MAX_FILE // 1024 // 1024} MB，请先缩小后再读取。")
        data = source.read_bytes()
    else:
        data = source
    if len(data) > MAX_FILE:
        raise ImageError("image_too_large", f"图片超过 {MAX_FILE // 1024 // 1024} MB，请先缩小后再读取。")
    media_type = sniff(data[:32])
    if media_type is None:
        raise ImageError("image_invalid", "不是可识别的图片（支持 PNG、JPEG、GIF、WebP、BMP）。")

    from PIL import Image, ImageOps

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image = Image.open(io.BytesIO(data))
            image.load()  # The first frame of an animation.
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ImageError("image_too_large", "图片像素过多，请先缩小后再读取。") from None
    except Exception:
        raise ImageError("image_invalid", "图片无法解码，文件可能已损坏或不完整。") from None

    width, height = image.size
    animated = getattr(image, "is_animated", False)
    rotated = image.getexif().get(ORIENTATION, 1) not in (1, None)
    oversized = max(width, height) > MAX_SIDE or encoded_size(len(data)) > MAX_ENCODED

    def processed(payload: bytes, kind: str, size: tuple[int, int]) -> ProcessedImage:
        return ProcessedImage(payload, kind, size[0], size[1], width, height, len(data))

    if media_type in SENDABLE and not animated and not rotated and not oversized:
        return processed(data, media_type, (width, height))
    try:
        image = ImageOps.exif_transpose(image) if rotated else image
        scale = min(1.0, MAX_SIDE / max(image.size))
        size = (max(1, round(image.size[0] * scale)), max(1, round(image.size[1] * scale)))
        for _ in range(SHRINK_ROUNDS + 1):
            scaled = image.resize(size, Image.Resampling.LANCZOS) if size != image.size else image
            png, jpeg = _encode(scaled, "image/png"), _encode(scaled, "image/jpeg", JPEG_QUALITIES[0])
            kind, payload = ("image/png", png) if len(png) <= len(jpeg) else ("image/jpeg", jpeg)
            if encoded_size(len(payload)) <= MAX_ENCODED:
                return processed(payload, kind, size)
            for quality in JPEG_QUALITIES[1:]:
                payload = _encode(scaled, "image/jpeg", quality)
                if encoded_size(len(payload)) <= MAX_ENCODED:
                    return processed(payload, "image/jpeg", size)
            size = (max(1, size[0] * 3 // 4), max(1, size[1] * 3 // 4))
    except ImageError:
        raise
    except Exception:
        raise ImageError("image_invalid", "图片无法处理，文件可能已损坏或不完整。") from None
    raise ImageError("image_too_large", "图片压缩后仍然过大，请先缩小后再读取。")


def _encode(image, media_type: str, quality: int = 80) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    if media_type == "image/jpeg":
        if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
            # JPEG has no transparency: put it on white, as a viewer would show it.
            rgba = image.convert("RGBA")
            background = Image.new("RGB", rgba.size, (255, 255, 255))
            background.paste(rgba, mask=rgba.getchannel("A"))
            image = background
        elif image.mode != "RGB":
            image = image.convert("RGB")
        image.save(buffer, "JPEG", quality=quality)
    else:
        if image.mode not in ("RGB", "RGBA", "L", "LA", "P", "1"):
            image = image.convert("RGBA")
        image.save(buffer, "PNG")
    return buffer.getvalue()
