"""Check whether an image is a bill (weighment slip) or a geotagged photo, and read its time.

Jev answers questions with probabilities instead of generating text, so each part of a time
(hour, minute, day, month) is a separate Choice question, assembled here. A first request also
asks which way the image is turned; if it is sideways, it is rotated upright and asked again.

Every image is asked both the bill questions and the photo questions at once (they run in
parallel); each states its premise ("If `image` is a bill, ..."), and code keeps only the
answers for the kind Jev found. Each time and date question can answer `not_shown`.

Usage:
    .venv/bin/python check_proof.py bill.png photo.png [--model jev-preview]

The same checks run behind the test page in app.py and the accuracy report in evaluate.py.
"""

import argparse
import sys
from pathlib import Path

from PIL import Image as PILImage
from PIL import ImageOps
from typesafe_sdk import Choice, Image, TypeSafeClient

PRICE_PER_INPUT_TOKEN = 0.042 / 1_000_000  # USD; output tokens are free
MIN_SIDE, MAX_SIDE = 280, 1568  # API limits for each side of an image
HEADERS = {"TypeSafe-Danger-Zone-Images": "true"}  # images are opt-in per request
NOT_SHOWN = "not_shown"


def options(values: list[str], not_shown: str) -> dict:
    """Choice criteria: one option per value, plus a no-match option so Jev is never forced to guess."""
    return {**{v: None for v in values}, NOT_SHOWN: not_shown}


HOURS = [f"{h:02d}" for h in range(24)]
MINUTES = [f"{m:02d}" for m in range(60)]
DAYS = [f"{d:02d}" for d in range(1, 32)]
MONTHS = [f"{m:02d}" for m in range(1, 13)]
TO_24H = "Give the hour on a 24-hour clock: 2:15 AM is 02, 2:15 PM is 14, 12:30 AM is 00."
PHOTO = "If `image` is a photo with a GPS stamp printed on it,"
BILL = "If `image` is a bill,"
NO_STAMP_TIME = "There is no GPS stamp with a time, or its time can't be read."
NO_STAMP_DATE = "There is no GPS stamp with a date, or its date can't be read."
NO_TIME_IN = "No time in is printed on a bill, or it can't be read."
NO_TIME_OUT = "No second, later time is printed on a bill, or it can't be read."
NO_BILL_DATE = "No date is printed on a bill, or it can't be read."

# How to turn the image so it is upright, by the answer to `orientation` (PIL rotates counterclockwise).
ROTATION = {"upright": 0, "top_points_right": 90, "upside_down": 180, "top_points_left": 270}

QUESTIONS = {
    "kind": Choice(
        instructions="What is `image`?",
        criteria={
            "bill": "A printed bill, receipt, invoice or weighment slip.",
            "geotagged_photo": (
                "A photo with a GPS stamp printed on it: a place name or address, latitude and "
                "longitude, and a date and time, as added by a GPS map camera app."
            ),
            "other": "Any other photo or document, including a photo with no GPS stamp.",
        },
    ),
    "orientation": Choice(
        instructions="Which way is the printed text in `image` turned?",
        criteria={
            "upright": "The text reads normally, left to right.",
            "top_points_right": "The image is turned so the tops of the letters face the right edge.",
            "top_points_left": "The image is turned so the tops of the letters face the left edge.",
            "upside_down": "The text is upside down.",
        },
    ),
    # Geotagged photo: the time and date in the GPS stamp.
    "stamp_hour": Choice(instructions=f"{PHOTO} what hour is the time in the stamp? {TO_24H}", criteria=options(HOURS, NO_STAMP_TIME)),
    "stamp_minute": Choice(instructions=f"{PHOTO} what minute is the time in the stamp?", criteria=options(MINUTES, NO_STAMP_TIME)),
    "stamp_day": Choice(instructions=f"{PHOTO} what day of the month is the date in the stamp?", criteria=options(DAYS, NO_STAMP_DATE)),
    "stamp_month": Choice(instructions=f"{PHOTO} what month (01 to 12) is the date in the stamp?", criteria=options(MONTHS, NO_STAMP_DATE)),
    # Bill: the times the vehicle came in and went out, and the date.
    "in_hour": Choice(
        instructions=f"{BILL} what hour is its time in (the first, earlier time printed)? {TO_24H}",
        criteria=options(HOURS, NO_TIME_IN),
    ),
    "in_minute": Choice(
        instructions=f"{BILL} what minute is its time in (the first, earlier time printed)?",
        criteria=options(MINUTES, NO_TIME_IN),
    ),
    "out_hour": Choice(
        instructions=f"{BILL} what hour is its time out (the second, later time printed)? {TO_24H}",
        criteria=options(HOURS, NO_TIME_OUT),
    ),
    "out_minute": Choice(
        instructions=f"{BILL} what minute is its time out (the second, later time printed)?",
        criteria=options(MINUTES, NO_TIME_OUT),
    ),
    "bill_day": Choice(instructions=f"{BILL} what day of the month is the date printed on it?", criteria=options(DAYS, NO_BILL_DATE)),
    "bill_month": Choice(instructions=f"{BILL} what month (01 to 12) is the date printed on it?", criteria=options(MONTHS, NO_BILL_DATE)),
}

# Which readings to report for each kind, as (key, label, [question ids joined by the separator], separator).
REPORT = {
    "geotagged_photo": [
        ("photo_time", "photo time", ["stamp_hour", "stamp_minute"], ":"),
        ("photo_date", "photo date", ["stamp_day", "stamp_month"], "/"),
    ],
    "bill": [
        ("time_in", "time in", ["in_hour", "in_minute"], ":"),
        ("time_out", "time out", ["out_hour", "out_minute"], ":"),
        ("bill_date", "bill date", ["bill_day", "bill_month"], "/"),
    ],
}


def fit(img: PILImage.Image) -> PILImage.Image:
    """Scale the longest side to the API maximum (small text reads better larger), padding if too narrow."""
    scale = MAX_SIDE / max(img.size)
    img = img.resize((round(img.width * scale), round(img.height * scale)), PILImage.LANCZOS)
    if min(img.size) < MIN_SIDE:
        canvas = PILImage.new("RGB", (max(img.width, MIN_SIDE), max(img.height, MIN_SIDE)), "white")
        canvas.paste(img, ((canvas.width - img.width) // 2, (canvas.height - img.height) // 2))
        img = canvas
    return img


def ask(client: TypeSafeClient, img: PILImage.Image, model: str):
    response = client.system_one(
        state={"image": Image.from_pil(fit(img), format="jpeg")},
        questions=QUESTIONS,
        model=model,
    )
    return response, response.choices


def top(answer, n: int = 3) -> list[tuple[str, float]]:
    return sorted(answer.probabilities.items(), key=lambda kv: kv[1], reverse=True)[:n]


def summary(answer) -> dict:
    return {"choice": answer.choice, "confidence": answer.confidence, "top": top(answer, 5)}


def readings_for(kind: str, answers: dict[str, dict]) -> list[dict]:
    """Put the answers for one kind of image together into readings such as time in "03:09".

    A reading with any part answered `not_shown` has value None and missing True.
    """
    readings = []
    for key, label, qids, sep in REPORT.get(kind, []):
        parts = [{"id": q, **answers[q]} for q in qids]
        missing = any(p["choice"] == NOT_SHOWN for p in parts)
        readings.append({
            "key": key,
            "label": label,
            "value": None if missing else sep.join(p["choice"] for p in parts),
            "missing": missing,
            "parts": parts,
            "min_confidence": min(p["confidence"] for p in parts),
        })
    return readings


def analyze(client: TypeSafeClient, img: PILImage.Image, model: str) -> tuple[dict, PILImage.Image]:
    """Check one upright-by-EXIF RGB image. Returns the result and the image as finally sent."""
    response, a = ask(client, img, model)
    tokens = response.usage.input_tokens or 0
    orientation = summary(a["orientation"])
    turn = ROTATION[a["orientation"].choice]
    if turn:
        img = img.rotate(turn, expand=True)
        response, a = ask(client, img, model)
        tokens += response.usage.input_tokens or 0

    answers = {q: summary(a[q]) for q in QUESTIONS}
    result = {
        "model": response.model,
        "requests": 2 if turn else 1,
        "orientation": orientation,
        "rotated": turn,
        "kind": answers["kind"],
        "readings": readings_for(a["kind"].choice, answers),
        "answers": answers,
        "input_tokens": tokens,
        "cost_usd": tokens * PRICE_PER_INPUT_TOKEN,
    }
    return result, fit(img)


def fmt_top(pairs) -> str:
    return ", ".join(f"{k}={p:.2f}" for k, p in pairs[:3])


def check(client: TypeSafeClient, path: Path, model: str) -> None:
    with PILImage.open(path) as original:
        img = ImageOps.exif_transpose(original).convert("RGB")
    r, _ = analyze(client, img, model)

    o, k = r["orientation"], r["kind"]
    print(f"\n{path}  (answered by {r['model']})")
    print(f"  orientation  {o['choice']:<16} confidence {o['confidence']:.2f}   top: {fmt_top(o['top'])}")
    if r["rotated"]:
        print(f"  -> rotated {r['rotated']}° and asked again")
    print(f"  kind         {k['choice']:<16} confidence {k['confidence']:.2f}   top: {fmt_top(k['top'])}")
    for reading in r["readings"]:
        detail = "   ".join(f"{p['id']}: {p['confidence']:.2f} ({fmt_top(p['top'][:2])})" for p in reading["parts"])
        print(f"  {reading['label']:<12} {reading['value'] or 'not shown':<16} {detail}")
    print(f"  usage: {r['input_tokens']} input tokens ≈ ${r['cost_usd']:.6f}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("images", nargs="+", type=Path)
    parser.add_argument("--model", default="jev-latest", help="jev-latest (1.13), jev-preview (1.14), or a pinned ID")
    args = parser.parse_args()

    with TypeSafeClient(headers=HEADERS) as client:
        for path in args.images:
            try:
                check(client, path, args.model)
            except Exception as error:  # keep going through the batch
                print(f"\n{path}: {type(error).__name__}: {error}", file=sys.stderr)


if __name__ == "__main__":
    main()
