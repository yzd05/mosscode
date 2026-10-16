"""Render exported Textual cells with Pillow for visual inspection on Windows."""

import json
from pathlib import Path
import sys
import unicodedata

from PIL import Image, ImageDraw, ImageFont


def render(path: Path):
    rows = json.loads(path.read_text(encoding="utf-8"))
    cell_width, cell_height = 12, 24
    def width(text):
        return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)
    image = Image.new("RGB", (max(sum(width(s["text"]) for s in r) for r in rows) * cell_width,
                              len(rows) * cell_height), "#0c0c0c")
    draw = ImageDraw.Draw(image)
    fonts = Path("C:/Windows/Fonts")
    normal = ImageFont.truetype(str(fonts / "consola.ttf"), 19)
    bold = ImageFont.truetype(str(fonts / "consolab.ttf"), 19)
    chinese = ImageFont.truetype(str(fonts / "msyh.ttc"), 17)
    symbols = ImageFont.truetype(str(fonts / "seguisym.ttf"), 19)
    for row_index, row in enumerate(rows):
        x, y = 0, row_index * cell_height
        for segment in row:
            color = segment["fg"] or "#deded7"
            for char in segment["text"]:
                step = width(char) * cell_width
                if segment["bg"]:
                    draw.rectangle((x, y, x + step, y + cell_height), fill=segment["bg"])
                if char in "█▀▄":
                    top = y + (cell_height // 2 if char == "▄" else 0)
                    bottom = y + (cell_height // 2 if char == "▀" else cell_height)
                    draw.rectangle((x, top, x + step - 1, bottom - 1), fill=color)
                elif char == "●":
                    draw.ellipse((x + 2, y + 8, x + 10, y + 16), fill=color)
                else:
                    font = chinese if ord(char) > 0x3000 else symbols if char in "❯✓✻└≥—" else bold if segment["bold"] else normal
                    draw.text((x, y + 1), char, font=font, fill=color)
                x += step
    image.save(path.with_suffix(".png"))


if __name__ == "__main__":
    for filename in sys.argv[1:]:
        render(Path(filename))
