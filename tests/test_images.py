"""Recognizing, normalizing and estimating images."""

import base64
import io
import os

import pytest
from PIL import Image

from mosscode import images
from mosscode.images import ImageError, estimate_image_tokens, load_image, sniff


def encode(image: Image.Image, fmt: str, **options) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, fmt, **options)
    return buffer.getvalue()


def noise(width: int, height: int) -> Image.Image:
    """Incompressible pixels, for images that must stay large once encoded."""
    return Image.frombytes("RGB", (width, height), os.urandom(width * height * 3))


@pytest.mark.parametrize(
    "fmt,media_type",
    [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("GIF", "image/gif"), ("WEBP", "image/webp")],
)
def test_supported_images_within_limits_are_sent_unchanged(fmt, media_type):
    data = encode(Image.new("RGB", (64, 32), "red"), fmt)
    image = load_image(data)
    assert image.data == data and image.media_type == media_type
    assert (image.width, image.height) == (64, 32) and not image.resized
    assert image.original_size == len(data) and image.name.endswith("." + images.EXTENSIONS[media_type])


def test_the_type_comes_from_the_header_not_the_extension(tmp_path):
    path = tmp_path / "photo.png"
    path.write_bytes(encode(Image.new("RGB", (8, 8)), "JPEG"))
    assert load_image(path).media_type == "image/jpeg"
    assert sniff(b"plain text") is None


def test_bmp_is_converted_to_png():
    image = load_image(encode(Image.new("RGB", (40, 30), "blue"), "BMP"))
    assert image.media_type == "image/png" and image.data.startswith(b"\x89PNG")
    assert (image.width, image.height) == (40, 30)


def test_exif_rotation_is_applied():
    exif = Image.Exif()
    exif[images.ORIENTATION] = 6  # Rotated 90° clockwise when shown.
    data = encode(Image.new("RGB", (60, 20)), "JPEG", exif=exif)
    image = load_image(data)
    assert (image.width, image.height) == (20, 60) and image.data != data
    assert Image.open(io.BytesIO(image.data)).size == (20, 60)


def test_animated_images_keep_only_the_first_frame():
    frames = [Image.new("RGB", (30, 30), color) for color in ("red", "green", "blue")]
    buffer = io.BytesIO()
    frames[0].save(buffer, "GIF", save_all=True, append_images=frames[1:])
    image = load_image(buffer.getvalue())
    decoded = Image.open(io.BytesIO(image.data))
    assert not getattr(decoded, "is_animated", False)
    assert decoded.convert("RGB").getpixel((0, 0)) == (255, 0, 0)


def test_large_images_are_scaled_to_the_longest_side_limit():
    data = encode(Image.new("RGB", (3840, 2160), "white"), "PNG")
    image = load_image(data)
    assert (image.width, image.height) == (2000, 1125)
    assert (image.original_width, image.original_height) == (3840, 2160) and image.resized
    assert Image.open(io.BytesIO(image.data)).size == (2000, 1125)
    assert image.describe().startswith("3840×2160 · ") and "（发送尺寸 2000×1125）" in image.describe()


def test_images_over_the_encoded_limit_are_recompressed_within_it():
    data = encode(noise(1900, 1000), "PNG")
    assert images.encoded_size(len(data)) > images.MAX_ENCODED
    image = load_image(data)
    assert images.encoded_size(len(image.data)) <= images.MAX_ENCODED
    assert len(base64.b64encode(image.data)) == images.encoded_size(len(image.data))
    assert image.media_type == "image/jpeg"


def test_images_that_never_fit_are_too_large(monkeypatch):
    monkeypatch.setattr(images, "MAX_ENCODED", 100)
    with pytest.raises(ImageError) as error:
        load_image(encode(noise(200, 200), "PNG"))
    assert error.value.code == "image_too_large"


def test_files_over_the_size_limit_are_refused_before_reading(tmp_path, monkeypatch):
    path = tmp_path / "big.png"
    path.write_bytes(encode(Image.new("RGB", (8, 8)), "PNG"))
    monkeypatch.setattr(images, "MAX_FILE", 10)
    with pytest.raises(ImageError) as error:
        load_image(path)
    assert error.value.code == "image_too_large"


def test_decompression_bombs_are_too_large(monkeypatch):
    data = encode(Image.new("1", (4000, 4000)), "PNG")
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1_000_000)
    with pytest.raises(ImageError) as error:
        load_image(data)
    assert error.value.code == "image_too_large"


@pytest.mark.parametrize("data", [b"not an image", b"\x89PNG\r\n\x1a\n" + b"\x00" * 40])
def test_unrecognized_or_corrupt_images_are_invalid(data):
    with pytest.raises(ImageError) as error:
        load_image(data)
    assert error.value.code == "image_invalid"


def test_truncated_images_are_invalid():
    data = encode(noise(100, 100), "PNG")
    with pytest.raises(ImageError) as error:
        load_image(data[: len(data) // 2])
    assert error.value.code == "image_invalid"


def test_transparent_images_compressed_to_jpeg_are_on_white():
    image = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
    jpeg = Image.open(io.BytesIO(images._encode(image, "image/jpeg")))
    assert jpeg.mode == "RGB" and jpeg.getpixel((5, 5)) == (255, 255, 255)


@pytest.mark.parametrize("size,tokens", [((750, 1), 1), ((100, 100), 14), ((1000, 1000), 1334), ((2000, 1125), 1600)])
def test_image_tokens_are_area_over_750_capped(size, tokens):
    assert estimate_image_tokens(*size) == tokens


def test_image_refs_read_memory_or_file(tmp_path):
    processed = load_image(encode(Image.new("RGB", (4, 4)), "PNG"))
    ref = images.image_ref(processed, "[图片 #1]")
    assert ref.read() == processed.data and ref.base64() == base64.b64encode(processed.data).decode()
    path = tmp_path / ref.name
    path.write_bytes(processed.data)
    stored = images.ImageRef(ref.name, ref.media_type, 4, 4, ref.size, ref.alt, path=path)
    assert stored == ref and stored.read() == processed.data
    assert stored.placeholder("当前模型不支持图片，已省略") == "[图片 #1]（当前模型不支持图片，已省略）"
