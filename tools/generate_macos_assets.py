"""Build the macOS installer background. The script runs only while packaging."""

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont


ACCENT = (255, 199, 0)


def _font(size: int):
    names = (
        "/System/Library/Fonts/SFNS.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/arial.ttf",
    )
    for name in names:
        path = Path(name)
        if path.exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _gradient(size, top, bottom):
    width, height = size
    image = Image.new("RGBA", size)
    draw = ImageDraw.Draw(image)
    for y in range(height):
        ratio = y / max(height - 1, 1)
        color = tuple(round(top[index] * (1 - ratio) + bottom[index] * ratio) for index in range(3))
        draw.line((0, y, width, y), fill=(*color, 255))
    return image


def _centered(draw, y, text, font, fill, width):
    bounds = draw.textbbox((0, 0), text, font=font)
    draw.text(((width - (bounds[2] - bounds[0])) / 2, y), text, font=font, fill=fill)


def create_dmg_background(output: Path) -> None:
    width, height = 720, 440
    canvas = _gradient((width, height), (36, 36, 36), (13, 13, 13))
    glow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((500, -180, 860, 180), fill=(*ACCENT, 90))
    canvas.alpha_composite(glow.filter(ImageFilter.GaussianBlur(85)))
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((44, 28, 676, 412), radius=30, fill=(12, 12, 12, 112), outline=(255, 255, 255, 25), width=1)
    draw.rounded_rectangle((72, 180, 648, 354), radius=26, fill=(28, 28, 28, 255), outline=(*ACCENT, 145), width=2)
    _centered(draw, 56, "Tripo Echo3D Bridge", _font(34), (255, 255, 255, 255), width)
    _centered(draw, 105, "Drag the app to Applications", _font(17), (220, 220, 220, 255), width)
    arrow_y = 255
    draw.line((296, arrow_y, 405, arrow_y), fill=(*ACCENT, 255), width=8)
    draw.polygon(((430, arrow_y), (400, arrow_y - 18), (400, arrow_y + 18)), fill=(*ACCENT, 255))
    _centered(draw, 378, "Drag to Applications to install", _font(16), (255, 255, 255, 255), width)
    canvas.convert("RGB").save(output, quality=95)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    create_dmg_background(args.output)
    print(args.output)


if __name__ == "__main__":
    main()
