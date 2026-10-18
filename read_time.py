"""Read the time shown on a clock in an image, using TypeSafe's Jev model.

Jev answers questions with probabilities instead of generating text, so the time is
read as separate Choice questions (hour, minute, AM/PM) and assembled here.

Usage:
    .venv/bin/python read_time.py photo.jpg [more.jpg ...] [--model jev-preview] [--expect 10:42]
"""

import argparse
import sys
from pathlib import Path

from PIL import Image as PILImage
from PIL import ImageOps
from typesafe_sdk import Choice, Image, TypeSafeClient

PRICE_PER_INPUT_TOKEN = 0.042 / 1_000_000  # USD; output tokens are free
MIN_SIDE, MAX_SIDE = 280, 1568  # API limits for each side of an image

QUESTIONS = {
    "clock_kind": Choice(
        instructions="What kind of clock or time display is in `image`?",
        criteria={
            "analog": "A clock face with hands.",
            "digital": "A display that shows the time as digits.",
            "none": "No clock or time display is visible.",
        },
    ),
    "hour": Choice(
        instructions=(
            "What hour does the clock in `image` show, on a 12-hour clock? "
            "On an analog clock, this is the number the short hour hand points at, "
            "or the last number it has passed. On a 24-hour display, 13 is 1 and 00 is 12."
        ),
        criteria={str(h): None for h in range(1, 13)},
    ),
    "minute": Choice(
        instructions=(
            "How many minutes past the hour does the clock in `image` show? "
            "On an analog clock, read the long minute hand: each number on the dial is "
            "5 minutes, so 12 is 00, 3 is 15, 6 is 30 and 9 is 45."
        ),
        criteria={f"{m:02d}": None for m in range(60)},
    ),
    "minute_hand": Choice(
        instructions=(
            "Which number on the dial is the long minute hand in `image` pointing nearest to? "
            "Answer none if this is not an analog clock."
        ),
        criteria={**{str(n): None for n in range(1, 13)}, "none": "Not an analog clock."},
    ),
    "period": Choice(
        instructions="Does the time display in `image` show whether it is AM or PM?",
        criteria={
            "am": "AM is shown, or a 24-hour display shows an hour from 00 to 11.",
            "pm": "PM is shown, or a 24-hour display shows an hour from 12 to 23.",
            "not_shown": "The display doesn't say, as on most analog clocks.",
        },
    ),
}


def prepare(path: Path) -> PILImage.Image:
    """Rotate upright and fit within the API's size limits, padding if an image is too narrow."""
    with PILImage.open(path) as original:
        img = ImageOps.exif_transpose(original).convert("RGB")
    w, h = img.size
    scale = min(MAX_SIDE / max(w, h), max(1.0, MIN_SIDE / min(w, h)))
    if scale != 1.0:
        img = img.resize((round(w * scale), round(h * scale)), PILImage.LANCZOS)
    if min(img.size) < MIN_SIDE:
        canvas = PILImage.new("RGB", (max(img.width, MIN_SIDE), max(img.height, MIN_SIDE)), "white")
        canvas.paste(img, ((canvas.width - img.width) // 2, (canvas.height - img.height) // 2))
        img = canvas
    return img


def top(answer, n: int = 3) -> str:
    best = sorted(answer.probabilities.items(), key=lambda kv: kv[1], reverse=True)[:n]
    return ", ".join(f"{k}={p:.2f}" for k, p in best)


def read_time(client: TypeSafeClient, path: Path, model: str, expect: str | None) -> None:
    img = prepare(path)
    response = client.system_one(
        state={"image": Image.from_pil(img, format="jpeg")},
        questions=QUESTIONS,
        model=model,
    )
    a = response.choices

    print(f"\n{path}  ({img.width}x{img.height}, answered by {response.model})")
    for qid in QUESTIONS:
        print(f"  {qid:<12} {a[qid].choice:<10} confidence {a[qid].confidence:.2f}   top: {top(a[qid])}")

    if a["clock_kind"].choice == "none":
        print("  => No clock found.")
        return
    period = {"am": " AM", "pm": " PM"}.get(a["period"].choice, "")
    reading = f"{int(a['hour'].choice)}:{a['minute'].choice}"
    print(f"  => Time: {reading}{period}")

    if expect:
        want_h, want_m = (int(x) for x in expect.split(":"))
        ok = (want_h - 1) % 12 + 1 == int(a["hour"].choice) and want_m == int(a["minute"].choice)
        print(f"  => Expected {expect}: {'CORRECT' if ok else 'WRONG'}")

    tokens = response.usage.input_tokens or 0
    print(f"  usage: {tokens} input tokens ≈ ${tokens * PRICE_PER_INPUT_TOKEN:.6f}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("images", nargs="+", type=Path)
    parser.add_argument("--model", default="jev-latest", help="jev-latest (1.13), jev-preview (1.14), or a pinned ID")
    parser.add_argument("--expect", help="expected time as H:MM, to check the reading (single image)")
    args = parser.parse_args()

    with TypeSafeClient(headers={"TypeSafe-Danger-Zone-Images": "true"}) as client:
        for path in args.images:
            try:
                read_time(client, path, args.model, args.expect)
            except Exception as error:  # keep going through the batch
                print(f"\n{path}: {type(error).__name__}: {error}", file=sys.stderr)


if __name__ == "__main__":
    main()
