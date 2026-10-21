"""Check a trip's proof images against the vehicle number typed in: a vehicle photo and a weigh bill.

Vehicle photo: does it show a vehicle, is a GPS stamp printed on it, can the number plate be read,
and is the plate the typed vehicle number? Weigh bill: is it a weighbridge bill, and is the vehicle
number printed on it the typed one? Both: are they from the same date? And was the photo taken
between the arrival and end time typed in? The bill's own times aren't asked about, because the
vehicle can be weighed before it is loaded.

Jev can't read text out, it only picks from answers it is given, so the typed vehicle number goes
into the state and Jev judges whether the plate or bill shows that number. Each part of a time
(hour, minute, day, month) is a separate Choice question, assembled here. If Jev is sure an image
is turned, it is rotated upright and asked again. Code makes every decision from the answers
(`checks_for`, `trip_checks`, `verdict`); the thresholds below are placeholders until evaluate.py
has measured accuracy on real images.

Usage:
    .venv/bin/python check_proof.py KL05AF9341 --photo photo.png --bill bill.png --arrival 03:00 --end 09:00 [--model jev-preview]

The same checks run behind the test page in app.py and the accuracy report in evaluate.py.
"""

import argparse
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image as PILImage
from PIL import ImageOps
from typesafe_sdk import Choice, Image, Noul, NoulAnswer, NoulCriteria, TypeSafeClient

PRICE_PER_INPUT_TOKEN = 0.042 / 1_000_000  # USD; output tokens are free
MIN_SIDE, MAX_SIDE = 280, 1568  # API limits for each side of an image
HEADERS = {"TypeSafe-Danger-Zone-Images": "true"}  # images are opt-in per request
NOT_SHOWN = "not_shown"

# Placeholder thresholds: choose real ones from evaluate.py reports on labeled images.
CONFIDENT = 0.8  # a Choice answer below this confidence goes to a person
YES, NO = 0.8, 0.2  # a yes/no answer between these goes to a person
# Lower than CONFIDENT: a wrong turn only costs a second request, and then gives unsure answers that go to a person.
# The sample bill, photographed sideways, was rightly found turned at 0.56 and was read less surely unturned.
ROTATE = 0.5  # rotate an image and ask again when `orientation` is at least this confident

SLOTS = ("photo", "bill")

# The API key lives in the login keyring (GNOME Keyring), stored with KEY_STORE_COMMAND.
KEY_LOOKUP = ["secret-tool", "lookup", "service", "typesafe", "account", "api"]
KEY_STORE_COMMAND = "secret-tool store --label='TypeSafe API key' service typesafe account api"
NO_KEY = f"No API key found. Store it in the keyring by running this in a terminal: {KEY_STORE_COMMAND}"
_keyring_key: str | None = None


def api_key() -> str | None:
    """The TypeSafe API key: TYPESAFE_API_KEY if set, otherwise from the login keyring.

    The key is only ever handed to the client: never printed, written to a file or put in the environment.
    """
    global _keyring_key
    if key := os.environ.get("TYPESAFE_API_KEY"):
        return key
    if not _keyring_key:
        try:  # a locked keyring asks to be unlocked, so allow time for that
            found = subprocess.run(KEY_LOOKUP, capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired):
            return None
        _keyring_key = found.stdout.strip() or None
    return _keyring_key


def normalize_vehicle_number(text: str) -> str | None:
    """'kl 05 af-9341' -> 'KL05AF9341'. None if it doesn't look like a vehicle number."""
    number = re.sub(r"[\s.\-]", "", text or "").upper()
    return number if re.fullmatch(r"[A-Z0-9]{4,12}", number) else None


def parse_time(text: str | None) -> str | None:
    """'9:05' or '09.05' -> '09:05'. None if it isn't a time on a 24-hour clock."""
    found = re.fullmatch(r"([01]?\d|2[0-3])[:.]([0-5]\d)", (text or "").strip())
    return f"{int(found[1]):02d}:{found[2]}" if found else None


def parse_window(arrival: str | None, end: str | None) -> tuple[str, str] | None:
    """The typed arrival and end time as ("HH:MM", "HH:MM"), or None if neither was typed."""
    if not (arrival or "").strip() and not (end or "").strip():
        return None
    window = parse_time(arrival), parse_time(end)
    if None in window:
        raise ValueError("Type both the arrival and end time on a 24-hour clock, such as 03:00 and 09:30.")
    return window


def options(values: list[str], not_shown: str) -> dict:
    """Choice criteria: one option per value, plus a no-match option so Jev is never forced to guess."""
    return {**{v: None for v in values}, NOT_SHOWN: not_shown}


def orientation(slot: str) -> Choice:
    return Choice(
        instructions=f"Which way is the printed text in `{slot}` turned?",
        criteria={
            "upright": "The text reads normally, left to right.",
            "top_points_right": "The image is turned so the tops of the letters face the right edge.",
            "top_points_left": "The image is turned so the tops of the letters face the left edge.",
            "upside_down": "The text is upside down.",
        },
    )


HOURS = [f"{h:02d}" for h in range(24)]
MINUTES = [f"{m:02d}" for m in range(60)]
DAYS = [f"{d:02d}" for d in range(1, 32)]
MONTHS = [f"{m:02d}" for m in range(1, 13)]
TO_24H = "Give the hour on a 24-hour clock: 2:15 AM is 02, 2:15 PM is 14, 12:30 AM is 00."
STAMP = "If a GPS stamp is printed on `photo`,"
BILL = "If `bill` is a weighbridge bill,"
NO_STAMP_TIME = "There is no GPS stamp with a time, or its time can't be read."
NO_STAMP_DATE = "There is no GPS stamp with a date, or its date can't be read."
NO_BILL_DATE = "No date is printed on the bill, or it can't be read."
SAME_NUMBER = (
    "Compare only the letters and digits, ignoring spaces, hyphens, dots, line breaks and letter case. "
    "They are the same only if every letter and digit matches, in the same order."
)

# How to turn the image so it is upright, by the answer to `orientation` (PIL rotates counterclockwise).
ROTATION = {"upright": 0, "top_points_right": 90, "upside_down": 180, "top_points_left": 270}

# Jev can lean towards the first option of a Choice, so "same" is never first in the number questions.
QUESTIONS = {
    "photo": {
        "orientation": orientation("photo"),
        "shows_vehicle": Noul(instructions="Does `photo` show a motor vehicle, such as a truck, tipper, pickup or van?"),
        "gps_stamp": Noul(
            instructions="Is a GPS stamp printed on `photo`?",
            criteria=NoulCriteria(
                true=(
                    "A block of text printed on the photo by a GPS map camera app, with a place name or "
                    "address, latitude and longitude, and a date and time."
                ),
                false="No such block of text, or only a date and time with no place or coordinates.",
            ),
        ),
        "plate": Choice(
            instructions="How well can the registration number on the vehicle's number plate in `photo` be read?",
            criteria={
                "not_visible": "No number plate of the vehicle is in view.",
                "partly_readable": (
                    "A number plate is in view, but some of its letters or digits are hidden, blurred, dirty, "
                    "cut off or too small to read."
                ),
                "readable": "Every letter and digit on the number plate can be read.",
            },
        ),
        "plate_number": Choice(
            instructions=(
                "Is the registration number on the vehicle's number plate in `photo` the same as "
                f"`vehicle_number`? {SAME_NUMBER}"
            ),
            criteria={
                "different": "At least one letter or digit on the plate differs from `vehicle_number`, or is missing or extra.",
                "same": "The plate shows the same letters and digits as `vehicle_number`, in the same order.",
                "cannot_tell": "No number plate is in view, or it can't be read well enough to compare.",
            },
        ),
        "stamp_hour": Choice(instructions=f"{STAMP} what hour is the time in the stamp? {TO_24H}", criteria=options(HOURS, NO_STAMP_TIME)),
        "stamp_minute": Choice(instructions=f"{STAMP} what minute is the time in the stamp?", criteria=options(MINUTES, NO_STAMP_TIME)),
        "stamp_day": Choice(instructions=f"{STAMP} what day of the month is the date in the stamp?", criteria=options(DAYS, NO_STAMP_DATE)),
        "stamp_month": Choice(instructions=f"{STAMP} what month (01 to 12) is the date in the stamp?", criteria=options(MONTHS, NO_STAMP_DATE)),
    },
    "bill": {
        "orientation": orientation("bill"),
        "weigh_bill": Noul(
            instructions="Is `bill` a weighbridge bill (a weighment slip)?",
            criteria=NoulCriteria(
                true="A printed slip from a weighbridge that records a vehicle's weight, such as gross, tare and net weight.",
                false="Any other document or photo, including a receipt or invoice that records no vehicle weight.",
            ),
        ),
        "bill_number": Choice(
            instructions=(
                "Is the vehicle number printed on `bill` the same as `vehicle_number`? The bill may label it "
                f"Vehicle No, Truck No or Lorry No. {SAME_NUMBER}"
            ),
            criteria={
                "different": (
                    "The bill shows a vehicle number, and at least one of its letters or digits differs from "
                    "`vehicle_number`, or is missing or extra."
                ),
                "same": "The bill shows a vehicle number with the same letters and digits as `vehicle_number`, in the same order.",
                NOT_SHOWN: "No vehicle number is printed on the bill, or it can't be read well enough to compare.",
            },
        ),
        # Only the date: the bill's times aren't checked, because the vehicle can be weighed before loading.
        "bill_day": Choice(instructions=f"{BILL} what day of the month is the date printed on it?", criteria=options(DAYS, NO_BILL_DATE)),
        "bill_month": Choice(instructions=f"{BILL} what month (01 to 12) is the date printed on it?", criteria=options(MONTHS, NO_BILL_DATE)),
    },
}

# Which readings to report for each slot, as (key, label, [question ids joined by the separator], separator).
REPORT = {
    "photo": [
        ("photo_time", "photo time", ["stamp_hour", "stamp_minute"], ":"),
        ("photo_date", "photo date", ["stamp_day", "stamp_month"], "/"),
    ],
    "bill": [
        ("bill_date", "bill date", ["bill_day", "bill_month"], "/"),
    ],
}

# What each yes/no answer means, as (check label, what a yes shows, what a no shows).
NOUL_CHECKS = {
    "shows_vehicle": ("Vehicle in photo", "Shows a vehicle", "No vehicle in the photo"),
    "gps_stamp": ("GPS stamp", "GPS stamp printed on the photo", "No GPS stamp on the photo"),
    "weigh_bill": ("Weigh bill", "Is a weighbridge bill", "Not a weighbridge bill"),
}
# What each Choice answer means, as (check label, {option: (status, detail)}).
CHOICE_CHECKS = {
    "plate": ("Number plate", {
        "readable": ("pass", "Number plate can be read"),
        "partly_readable": ("review", "Number plate is only partly readable"),
        "not_visible": ("fail", "No number plate in view"),
    }),
    "plate_number": ("Plate matches {number}", {
        "same": ("pass", "Plate shows {number}"),
        "different": ("fail", "Plate shows a different number"),
        "cannot_tell": ("review", "Plate can't be compared"),
    }),
    "bill_number": ("Bill matches {number}", {
        "same": ("pass", "Bill shows {number}"),
        "different": ("fail", "Bill shows a different vehicle number"),
        NOT_SHOWN: ("review", "No vehicle number readable on the bill"),
    }),
}
TITLES = {"pass": "Accepted", "fail": "Rejected", "review": "Needs a person to check", "waiting": "Waiting for both images"}


def fit(img: PILImage.Image) -> PILImage.Image:
    """Scale the longest side to the API maximum (small text reads better larger), padding if too narrow."""
    scale = MAX_SIDE / max(img.size)
    img = img.resize((round(img.width * scale), round(img.height * scale)), PILImage.LANCZOS)
    if min(img.size) < MIN_SIDE:
        canvas = PILImage.new("RGB", (max(img.width, MIN_SIDE), max(img.height, MIN_SIDE)), "white")
        canvas.paste(img, ((canvas.width - img.width) // 2, (canvas.height - img.height) // 2))
        img = canvas
    return img


def ask(client: TypeSafeClient, slot: str, img: PILImage.Image, vehicle_number: str, model: str):
    return client.system_one(
        state={slot: Image.from_pil(fit(img), format="jpeg"), "vehicle_number": vehicle_number},
        questions=QUESTIONS[slot],
        model=model,
    )


def top(answer, n: int = 3) -> list[tuple[str, float]]:
    return sorted(answer.probabilities.items(), key=lambda kv: kv[1], reverse=True)[:n]


def summary(answer) -> dict:
    if isinstance(answer, NoulAnswer):
        return {"type": "noul", "yes": answer.noul}
    return {"type": "choice", "choice": answer.choice, "confidence": answer.confidence, "top": top(answer, 5)}


def readings_for(slot: str, answers: dict[str, dict]) -> list[dict]:
    """Put the time and date answers together into readings such as photo time "04:09".

    A reading with any part answered `not_shown` has value None and missing True.
    """
    readings = []
    for key, label, qids, sep in REPORT[slot]:
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


def checks_for(slot: str, answers: dict[str, dict], vehicle_number: str) -> list[dict]:
    """Turn one image's yes/no and Choice answers into pass, fail or review checks.

    `certainty` is the probability of yes for a yes/no answer (`measure` "yes"), and the Choice
    confidence otherwise.
    """
    checks = []
    for qid in QUESTIONS[slot]:
        a = answers[qid]
        if qid in NOUL_CHECKS:
            label, yes, no = NOUL_CHECKS[qid]
            status = "pass" if a["yes"] >= YES else "fail" if a["yes"] <= NO else "review"
            detail = {"pass": yes, "fail": no, "review": f"Unsure: {yes.lower()}?"}[status]
            checks.append({"key": qid, "label": label, "status": status, "detail": detail,
                           "certainty": a["yes"], "measure": "yes"})
        elif qid in CHOICE_CHECKS:
            label, outcomes = CHOICE_CHECKS[qid]
            status, detail = outcomes[a["choice"]]
            if a["confidence"] < CONFIDENT:
                status, detail = "review", f"Unsure: {detail[0].lower()}{detail[1:]}"
            checks.append({"key": qid, "label": label.format(number=vehicle_number), "status": status,
                           "detail": detail.format(number=vehicle_number), "certainty": a["confidence"],
                           "measure": "confidence"})
    return checks


def minutes(hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m


def within(t: str, start: str, end: str) -> bool:
    """Is time `t` from `start` to `end`? A window that ends earlier than it starts runs past midnight."""
    t, start, end = minutes(t), minutes(start), minutes(end)
    return start <= t <= end if start <= end else t >= start or t <= end


def reading_check(key: str, label: str, readings: list[dict], ok, yes: str, no: str) -> dict:
    """A check that compares readings in code. `ok` takes their values; `yes` and `no` format them."""
    missing = [x["label"] for x in readings if x["missing"]]
    if missing:
        return {"key": key, "label": label, "status": "review", "detail": "Not shown or unreadable: " + ", ".join(missing),
                "certainty": None, "measure": None}
    values = [x["value"] for x in readings]
    passed = ok(*values)
    certainty = min(x["min_confidence"] for x in readings)
    status = "review" if certainty < CONFIDENT else "pass" if passed else "fail"
    detail = (yes if passed else no).format(*values) + (" (unsure reading)" if status == "review" else "")
    return {"key": key, "label": label, "status": status, "detail": detail, "certainty": certainty, "measure": "confidence"}


def trip_checks(photo: dict | None, bill: dict | None, window: tuple[str, str] | None) -> list[dict]:
    """Photo and bill from the same date, and the photo taken between the typed arrival and end time.

    The bill's times aren't asked about: the vehicle can be weighed before it is loaded.
    """
    r = {x["key"]: x for x in (photo["readings"] if photo else []) + (bill["readings"] if bill else [])}
    checks = []
    if photo and bill:
        checks.append(reading_check("same_date", "Same date", [r["photo_date"], r["bill_date"]],
                                    lambda a, b: a == b, "Both dated {0}", "Photo {0}, bill {1}"))
    if photo and not window:
        checks.append({"key": "in_window", "label": "Photo during the trip", "status": "review",
                       "detail": "Arrival and end time not typed", "certainty": None, "measure": None})
    elif photo:
        arrival, end = window
        checks.append(reading_check("in_window", "Photo during the trip", [r["photo_time"]],
                                    lambda t: within(t, arrival, end),
                                    f"Photo {{0}} is between arrival {arrival} and end {end}",
                                    f"Photo {{0}} is outside arrival {arrival} to end {end}"))
    return checks


def verdict(photo: dict | None, bill: dict | None, window: tuple[str, str] | None = None) -> dict:
    """One decision for the trip: any failed check rejects it, any unsure check sends it to a person."""
    checks = (photo["checks"] if photo else []) + (bill["checks"] if bill else [])
    checks += trip_checks(photo, bill, window)
    statuses = {c["status"] for c in checks}
    if "fail" in statuses:
        status = "fail"
    elif not (photo and bill):
        status = "waiting"
    else:
        status = "review" if "review" in statuses else "pass"
    return {"status": status, "title": TITLES[status], "checks": checks}


def analyze(client: TypeSafeClient, slot: str, img: PILImage.Image, vehicle_number: str, model: str) -> tuple[dict, PILImage.Image]:
    """Check one upright-by-EXIF RGB image in its slot. Returns the result and the image as finally sent."""
    response = ask(client, slot, img, vehicle_number, model)
    tokens = response.usage.input_tokens or 0
    turned = response.choices["orientation"]
    turn = ROTATION[turned.choice] if turned.confidence >= ROTATE else 0
    if turn:
        img = img.rotate(turn, expand=True)
        response = ask(client, slot, img, vehicle_number, model)
        tokens += response.usage.input_tokens or 0

    answers = {q: summary(a) for q, a in response.answers.items()}
    result = {
        "slot": slot,
        "model": response.model,
        "requests": 2 if turn else 1,
        "orientation": summary(turned),
        "rotated": turn,
        "checks": checks_for(slot, answers, vehicle_number),
        "readings": readings_for(slot, answers),
        "answers": answers,
        "input_tokens": tokens,
        "cost_usd": tokens * PRICE_PER_INPUT_TOKEN,
    }
    return result, fit(img)


def analyze_trip(client: TypeSafeClient, images: dict[str, PILImage.Image], vehicle_number: str, model: str,
                 window: tuple[str, str] | None = None) -> tuple[dict, dict]:
    """Check each slot's image in parallel and decide. Returns ({slot: result, "verdict": ...}, {slot: image as sent}).

    `window` is the typed (arrival, end) time, which the photo time must fall between.
    """
    with ThreadPoolExecutor(len(images)) as pool:
        futures = {slot: pool.submit(analyze, client, slot, img, vehicle_number, model) for slot, img in images.items()}
        done = {slot: f.result() for slot, f in futures.items()}
    results = {slot: r for slot, (r, _) in done.items()}
    sent = {slot: img for slot, (_, img) in done.items()}
    return {**results, "verdict": verdict(results.get("photo"), results.get("bill"), window)}, sent


def load(path: Path) -> PILImage.Image:
    with PILImage.open(path) as original:
        return ImageOps.exif_transpose(original).convert("RGB")


MARK = {"pass": "ok  ", "fail": "FAIL", "review": "??  "}


def print_trip(trip: dict, vehicle_number: str) -> None:
    for slot in SLOTS:
        r = trip.get(slot)
        if not r:
            continue
        rotated = f", rotated {r['rotated']}°" if r["rotated"] else ""
        print(f"\n{slot}  (answered by {r['model']}{rotated})")
        for c in r["checks"]:
            print(f"  {MARK[c['status']]} {c['label']:<26} {c['detail']}  ({c['certainty']:.2f})")
        for reading in r["readings"]:
            print(f"       {reading['label']:<26} {reading['value'] or 'not shown'}  ({reading['min_confidence']:.2f})")
        print(f"       {r['input_tokens']} input tokens ≈ ${r['cost_usd']:.6f}")
    v = trip["verdict"]
    print(f"\nVehicle {vehicle_number}: {v['title']}")
    for c in v["checks"]:
        if c["status"] != "pass" or c["key"] in ("same_date", "in_window"):
            print(f"  {MARK[c['status']]} {c['label']}: {c['detail']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("vehicle_number", help="the trip's vehicle number, such as KL05AF9341")
    parser.add_argument("--photo", type=Path, help="photo of the vehicle with a GPS stamp")
    parser.add_argument("--bill", type=Path, help="weighbridge bill")
    parser.add_argument("--arrival", help="the time the vehicle arrived, such as 03:00")
    parser.add_argument("--end", help="the time the trip ended, such as 09:30 (earlier than --arrival if past midnight)")
    parser.add_argument("--model", default="jev-latest", help="jev-latest (1.13), jev-preview (1.14), or a pinned ID")
    args = parser.parse_args()

    number = normalize_vehicle_number(args.vehicle_number)
    if not number:
        parser.error(f"{args.vehicle_number!r} doesn't look like a vehicle number. Use 4 to 12 letters and digits.")
    try:
        window = parse_window(args.arrival, args.end)
    except ValueError as error:
        parser.error(str(error))
    images = {slot: load(path) for slot in SLOTS if (path := getattr(args, slot))}
    if not images:
        parser.error("Give --photo, --bill, or both.")

    if not (key := api_key()):
        sys.exit(NO_KEY)
    with TypeSafeClient(api_key=key, headers=HEADERS) as client:
        try:
            trip, _ = analyze_trip(client, images, number, args.model, window)
        except Exception as error:
            sys.exit(f"{type(error).__name__}: {error}")
    print_trip(trip, number)


if __name__ == "__main__":
    main()
