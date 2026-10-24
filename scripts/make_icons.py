#!/usr/bin/env python3
"""Generate placeholder Tauri icons (32x32, 128x128, icon.png 256, icon.ico).

Draws a teal rounded square with a cream «د» glyph. Pure Pillow, deterministic.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ICONS = Path(__file__).resolve().parent.parent / "desktop" / "src-tauri" / "icons"
TEAL = (14, 124, 107, 255)
CREAM = (246, 243, 236, 255)


def find_font(size: int):
    for p in (
        "/usr/share/fonts/truetype/chinese/NotoSansSC-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def make(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = size // 5
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=TEAL)
    # draw the Persian letter «د» as a bold arc-like glyph using DejaVu (has it)
    font = find_font(int(size * 0.62))
    try:
        bbox = d.textbbox((0, 0), "د", font=font)
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        d.text(((size - w) / 2 - bbox[0], (size - h) / 2 - bbox[1]), "د",
               font=font, fill=CREAM)
    except Exception:
        d.ellipse([size*0.3, size*0.3, size*0.7, size*0.7], fill=CREAM)
    return img


def main() -> None:
    ICONS.mkdir(parents=True, exist_ok=True)
    sizes = {"32x32.png": 32, "128x128.png": 128, "icon.png": 256}
    for name, s in sizes.items():
        make(s).save(ICONS / name)
        print("wrote", ICONS / name)
    # multi-size .ico for Windows
    img = make(256)
    img.save(ICONS / "icon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("wrote", ICONS / "icon.ico")


if __name__ == "__main__":
    main()
