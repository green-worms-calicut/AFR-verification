"""Draw an analog and a digital clock at a known time, for testing read_time.py.

Usage:
    .venv/bin/python make_test_clock.py 10:42   # writes samples/analog-1042.jpg and samples/digital-1042.jpg
"""

import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SIZE = 800


def analog(hour: int, minute: int) -> Image.Image:
    img = Image.new("RGB", (SIZE, SIZE), "white")
    d = ImageDraw.Draw(img)
    c, r = SIZE / 2, SIZE * 0.45
    d.ellipse((c - r, c - r, c + r, c + r), outline="black", width=10)
    font = ImageFont.load_default(size=56)

    def point(angle_deg: float, length: float) -> tuple[float, float]:
        a = math.radians(angle_deg - 90)  # 0 degrees is 12 o'clock
        return c + length * math.cos(a), c + length * math.sin(a)

    for tick in range(60):
        inner = r * (0.85 if tick % 5 == 0 else 0.92)
        d.line((point(tick * 6, inner), point(tick * 6, r * 0.97)), fill="black", width=6 if tick % 5 == 0 else 2)
    for n in range(1, 13):
        d.text(point(n * 30, r * 0.72), str(n), fill="black", font=font, anchor="mm")

    d.line(((c, c), point((hour % 12 + minute / 60) * 30, r * 0.5)), fill="black", width=18)
    d.line(((c, c), point(minute * 6, r * 0.82)), fill="black", width=10)
    d.ellipse((c - 16, c - 16, c + 16, c + 16), fill="black")
    return img


def digital(hour: int, minute: int) -> Image.Image:
    img = Image.new("RGB", (SIZE, SIZE // 2), "black")
    d = ImageDraw.Draw(img)
    d.text((SIZE / 2, SIZE / 4), f"{hour:02d}:{minute:02d}", fill="#ff3b30", font=ImageFont.load_default(size=200), anchor="mm")
    return img


def main() -> None:
    hour, minute = (int(x) for x in sys.argv[1].split(":"))
    out = Path(__file__).parent / "samples"
    out.mkdir(exist_ok=True)
    for name, draw in (("analog", analog), ("digital", digital)):
        path = out / f"{name}-{hour:02d}{minute:02d}.jpg"
        draw(hour, minute).save(path, "JPEG", quality=92)
        print(path)


if __name__ == "__main__":
    main()
