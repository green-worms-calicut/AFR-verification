"""Check a whole trip's proof images against what was typed in: loading photos, unloading photos and weigh bills.

A trip runs one of two ways, worked out from which weighing came first:
  - tare first: tare -> loading -> gross -> unloading. The empty vehicle is weighed (tare), loaded and
    photographed, weighed loaded (gross), and photographed again where it unloads;
  - gross first: loading -> gross -> unloading -> tare. The vehicle is loaded and photographed, weighed
    loaded at the plant's weighbridge (gross), unloaded and photographed, then weighed empty (tare).

Photos (each loading and unloading photo): does it show a vehicle, is a GPS stamp printed on it, is
it a photo taken directly rather than a photo of a screen, and does one photo of each kind clearly
show the typed vehicle number? Weigh bills: is there a weighbridge slip (handwritten notes are
ignored), a final slip with both weights, the typed vehicle number on every slip, the typed
quantity as the final slip's net weight, and gross heavier than tare?

Times, from the final slip's tare and gross times and the photos' GPS stamps:
  - tare first: the weighings at least `min_gap_tare_first` apart, every loading photo between them,
    and every unloading photo after gross;
  - gross first: the weighings at least `min_gap_gross_first` apart, every loading photo before gross,
    and every unloading photo between gross and tare.
A photo's clock may be off the weighbridge's by up to `clock_slack`.

Place: when the plant's location is given (`--plant`, never fixed in code), the latitude and longitude
on each unloading photo's GPS stamp are read to two decimals, and at least one must place the photo
within `plant_radius` of the plant; any clearly farther is flagged. Each run can change these limits (`Limits`).

Stamp, for a Verra project trip (`--verra`): the final slip must carry the weighbridge's own ink stamp. Our
company's name (`OUR_COMPANY`) is sent so that our company's stamp, often on the slip too, isn't taken for it.

Across trips: an
image already used in another trip is flagged (`ImageIndex`), and so is a vehicle on two trips at once
(`overlap_checks`).

Nothing is rejected here. A check that finds a problem flags the trip, an unsure one asks a person
to look, and the person accepts or rejects it.

Jev can't read text out, it only picks from answers it is given, so the typed vehicle number and
quantity go into the state and Jev judges whether the plate or bill shows them. Each part of a time
(hour, minute, day, month) or of a GPS stamp's latitude and longitude (degrees and the first two
decimals) is a separate Choice question, assembled here. If Jev is sure an image
is turned, it is rotated upright and asked again. Code makes every decision from the answers
(`checks_for`, `trip_checks`, `verdict`); the thresholds below are placeholders until evaluate.py
and run_trips.py have measured accuracy on real images.

Usage:
    .venv/bin/python check_proof.py KL57AB8815 --trip trips/.../171773 --quantity 1630
    .venv/bin/python check_proof.py KL57AB8815 --loading c1.jpg c2.jpg --unloading u1.jpg u2.jpg \\
        --bill slip1.jpg slip2.jpg --quantity 1630 [--plant "11.4404, 75.9420" --plant-radius 500] [--verra] \\
        [--min-gap-tare-first 60] [--min-gap-gross-first 10] [--clock-slack 5] [--model jev-preview]

The same checks run behind the test page in app.py, the trip runner run_trips.py, and the accuracy
report in evaluate.py.
"""

import argparse
import hashlib
import io
import itertools
import json
import math
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import Path

from PIL import Image as PILImage
from PIL import ImageOps
from typesafe_sdk import Box, Choice, Image, Noul, NoulAnswer, NoulCriteria, TypeSafeClient

PRICE_PER_INPUT_TOKEN = 0.042 / 1_000_000  # USD; output tokens are free
MIN_SIDE, MAX_SIDE = 280, 1568  # API limits for each side of an image
HEADERS = {"TypeSafe-Danger-Zone-Images": "true"}  # images are opt-in per request
TIMEOUT = 60.0  # seconds per request; the SDK's 10 s default timed out on large slips in the 19 Oct 2026 run
NOT_SHOWN = "not_shown"

# Placeholder thresholds: choose real ones from evaluate.py reports on labeled images.
CONFIDENT = 0.8  # a Choice answer below this confidence goes to a person
YES, NO = 0.8, 0.2  # a yes/no answer between these goes to a person
# Thresholds measured for one question: (yes at or above, no at or below). In the 19 Oct 2026 run of 42 real
# trips, every photo taken directly scored 0.05-0.43 for screen_photo, and every photo of a screen 0.99.
NOUL_THRESHOLDS = {"screen_photo": (0.8, 0.6)}
# Lower than CONFIDENT: a wrong turn only costs a second request, and then gives unsure answers that go to a person.
# The sample bill, photographed sideways, was rightly found turned at 0.56 and was read less surely unturned.
ROTATE = 0.5  # rotate an image and ask again when `orientation` is at least this confident
# Sideways but unsure which way: in the 19 Oct 2026 run Jev put 89-95% on the two sideways options for
# sideways slips, split about 55/40 between them, so no one option reached ROTATE and 33 of 43 sideways
# slips were read sideways. Above this, the likelier way is tried, then checked (see `turn_for`).
SIDEWAYS = 0.7

SLOTS = ("photo", "bill")  # the two kinds of image Jev is asked about
ROLES = ("loading", "unloading", "bill")  # what each image is for in a trip
SLOT_OF = {"loading": "photo", "unloading": "photo", "bill": "bill"}
ROLE_TITLES = {"loading": "Loading photos", "unloading": "Unloading photos", "bill": "Weigh bills"}
IMAGE_TYPES = (".jpg", ".jpeg", ".png", ".heic")  # the .heic files seen so far were really JPEGs


@dataclass(frozen=True)
class Limits:
    """The timing rules, in minutes, and how near the plant unloading must be, in metres. The defaults can be changed for each run."""

    min_gap_tare_first: int = 60  # loading happens between the weighings, so they are at least this far apart
    min_gap_gross_first: int = 10  # unloading happens between the weighings, so they are at least this far apart
    clock_slack: int = 5  # the camera's clock and the weighbridge's may disagree by this much
    # How far from the plant an unloading photo's square (`plant_check`) may be. Plant photos on 19 Oct 2026
    # were stamped within 51 m of it; the nearest unloading elsewhere was 2.9 km away.
    plant_radius: int = 500


# How each limit is described in error messages and the command-line help, and its unit.
LIMIT_NAMES = {
    "min_gap_tare_first": "the least gap between the tare and gross weighings, when tare is first",
    "min_gap_gross_first": "the least gap between the gross and tare weighings, when gross is first",
    "clock_slack": "how far a photo's clock may be off the weighbridge's",
    "plant_radius": "how far from the plant an unloading photo's stamp may place it",
}
LIMIT_UNITS = {"min_gap_tare_first": "minutes", "min_gap_gross_first": "minutes", "clock_slack": "minutes", "plant_radius": "metres"}

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


def parse_limits(typed: dict) -> Limits:
    """Limits from typed whole numbers, such as {"clock_slack": "10"}. A blank or missing one keeps its default."""
    values = {}
    for f in fields(Limits):
        text = str(typed.get(f.name) or "").strip()
        if not text:
            continue
        if not re.fullmatch(r"\d{1,4}", text):
            raise ValueError(f"Type {LIMIT_NAMES[f.name]} as whole {LIMIT_UNITS[f.name]}, such as {f.default}.")
        values[f.name] = int(text)
    return Limits(**values)


def parse_quantity(text: str | None) -> str | None:
    """'2,570 kg' -> '2570', '480.50' -> '480.5'. None if nothing was typed.

    The quantity is in kilograms, as weighbridge bills print it.
    """
    text = str(text or "").strip()
    if not text:
        return None
    found = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:kg|kgs)?\.?", text.replace(",", ""), re.IGNORECASE)
    if not found or float(found[1]) <= 0:
        raise ValueError("Type the quantity in kilograms as a number, such as 480.")
    number = found[1].lstrip("0") or "0"
    if "." in number:
        number = number.rstrip("0").rstrip(".")
    return "0" + number if number.startswith(".") else number


def parse_plant(value) -> tuple[float, float] | None:
    """'11.4404, 75.9420' or [11.4404, 75.942] -> (11.4404, 75.942): the plant's latitude and longitude.

    In decimal degrees north and east, as Google Maps copies a place. None if nothing was given.
    """
    text = ", ".join(map(str, value)) if isinstance(value, (list, tuple)) else str(value or "")
    text = text.replace("°", "").strip()
    if not text:
        return None
    found = re.fullmatch(r"(\d{1,2}(?:\.\d+)?)\s*[,\s]\s*(\d{1,3}(?:\.\d+)?)", text)
    if not found or float(found[1]) > 90 or float(found[2]) > 180:
        raise ValueError("Type the plant's location as latitude, longitude in decimal degrees north and east, "
                         "such as 11.4404, 75.9420.")
    return float(found[1]), float(found[2])


def parse_switch(value, name: str) -> bool:
    """true, "yes", 1 -> True; false, "no", 0 or nothing -> False: a tick box, or a switch in trip.json."""
    text = str(value if value is not None else "").strip().lower()
    if text in ("true", "yes", "1"):
        return True
    if text in ("", "false", "no", "0", "none"):
        return False
    raise ValueError(f"Give {name} as true or false.")


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
NO_STAMP_TIME = "There is no GPS stamp with a time, or its time can't be read."
NO_STAMP_DATE = "There is no GPS stamp with a date, or its date can't be read."
# The two bill layouts: a date and time printed with each weight, or only a Time In and Time Out.
GROSS = "If `bill` prints a date and time for the gross (loaded) weight itself, next to or under it,"
TARE = "If `bill` prints a date and time for the tare (empty) weight itself, next to or under it,"
TIME_IN = "If `bill` prints a Time In (the first weighment, often beside a Date In),"
TIME_OUT = "If `bill` prints a Time Out (the second weighment, often beside a Date Out),"
NOT_CAMERA = "Ignore any timestamp on a camera picture printed on the slip."
NO_BILL_TIME = "The bill prints no such time, or it can't be read."
NO_BILL_DATE = "The bill prints no such date, or it can't be read."
SAME_NUMBER = (
    "Compare only the letters and digits, ignoring spaces, hyphens, dots, line breaks and letter case. "
    "They are the same only if every letter and digit matches, in the same order."
)


def bill_time(prefix: str, premise: str) -> dict:
    """The five Choice questions that read one date and time on the bill: `{prefix}_hour`, `_minute`, `_ampm`, `_day`, `_month`.

    The hour is read as printed and AM or PM is asked separately; code turns them into a 24-hour time
    (`to_24h`). Asked to convert "03:06:08 PM" itself, Jev often answered 03 (19 Oct 2026 run).
    """
    return {
        f"{prefix}_hour": Choice(instructions=f"{premise} what hour is that time, exactly as printed? If AM or PM is "
                                              f"printed after it, give the hour as printed, from 01 to 12. {NOT_CAMERA}",
                                 criteria=options(HOURS, NO_BILL_TIME)),
        f"{prefix}_minute": Choice(instructions=f"{premise} what minute is that time? {NOT_CAMERA}",
                                   criteria=options(MINUTES, NO_BILL_TIME)),
        f"{prefix}_ampm": Choice(instructions=f"{premise} is AM or PM printed after that time? {NOT_CAMERA}",
                                 criteria={"none": "Neither: the time is on a 24-hour clock, such as 15:06.",
                                           "am": "AM is printed after the time.", "pm": "PM is printed after the time.",
                                           NOT_SHOWN: NO_BILL_TIME}),
        f"{prefix}_day": Choice(instructions=f"{premise} what day of the month is its date? {NOT_CAMERA}",
                                criteria=options(DAYS, NO_BILL_DATE)),
        f"{prefix}_month": Choice(instructions=f"{premise} what month (01 to 12) is its date? {NOT_CAMERA}",
                                  criteria=options(MONTHS, NO_BILL_DATE)),
    }


def coordinate(prefix: str, axis: str, labels: str, most: int) -> dict:
    """The two Choice questions that read the GPS stamp's latitude or longitude to two decimals (about
    1.1 km): `{prefix}_degrees` and `{prefix}_decimals`, the first two digits after the point.

    Code puts them together (`position`) and measures how far that square is from the plant (`plant_check`).
    The third decimal isn't read: on the 88 unloading photos of 19 Oct 2026, Jev read the degrees right
    every time and the first two decimals 168 times in 176, but the longitude's third decimal only 47 times
    in 88 (often the second digit again), and 67 even when told the digits before it, 6 of the misses sure.
    """
    where = f"{STAMP} look at its {axis}, labelled {labels}, printed as a decimal number of degrees."
    unread = f"There is no GPS stamp with a {axis} printed as a decimal number, or it can't be read."
    return {
        f"{prefix}_degrees": Choice(instructions=f"{where} What whole number of degrees is before the decimal point?",
                                    criteria=options([str(d) for d in range(most + 1)], unread)),
        f"{prefix}_decimals": Choice(instructions=f"{where} What are the first two digits after the decimal point? "
                                                  "In 12.345678 they are 34.",
                                     criteria=options([f"{n:02d}" for n in range(100)], unread)),
    }


# Reading the position to four decimals (about 11 m) takes two more requests: find the stamp's Lat/Long line,
# then pick the value from candidates on that line cut out at full resolution (`read_line`). Asked for the
# third and fourth digits instead, Jev often gave the second again, even on the cut-out line.
BOX_MODEL = "jev-preview"  # Box questions need Jev 1.14
LINE = Box(instructions="Find the line of text in the GPS stamp printed on `photo` that gives the latitude and "
                        "longitude (Lat ... Long ...).")
PICKS = ("lat_pick", "long_pick")


def pick(axis: str, labels: str, starts: list[str]) -> Choice:
    """Which four-decimal value starting with one of `starts` (such as 11.44, or 11.44 and 11.40 when the
    two-decimal reading was torn) is the `line`'s latitude or longitude: 100 candidates per start.

    Picking a whole value works where reading single digits doesn't: on the 88 unloading photos of 19 Oct
    2026, cut-out lines were picked right 173 times in 176, and every pick at least 0.8 sure was right or
    was a sure "can't read".
    """
    begins = " or ".join(starts)
    return Choice(instructions=f"`line` is cut from a GPS stamp. Look at its {axis}, labelled {labels}. It starts with "
                               f"{begins}. Which of these is it, up to the fourth digit after the decimal point? "
                               "Ignore any digits after the fourth.",
                  criteria=options([f"{start}{n:02d}" for start in starts for n in range(100)],
                                   f"No {axis} starting with {begins} is printed, or it can't be read that far."))


def starts_of(answers: dict[str, dict], prefix: str) -> list[str]:
    """The likeliest two-decimal starts of a coordinate, such as ["11.44", "11.40"]: the degrees with the two
    likeliest first-two-decimals, the second only if at least 5% likely. Jev was often torn between 44 and 40.
    """
    degrees = answers[f"{prefix}_degrees"]["choice"]
    decimals = [option for i, (option, p) in enumerate(answers[f"{prefix}_decimals"]["top"][:2])
                if option != NOT_SHOWN and (i == 0 or p >= 0.05)]
    return [] if degrees == NOT_SHOWN else [f"{degrees}.{d}" for d in decimals]


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
                    "A block of text printed on the photo by a GPS camera app, with a place name or address, "
                    "latitude and longitude, and a date and time. It may have a map, a logo, or neither."
                ),
                false="No such block of text, or only a date and time with no place or coordinates.",
            ),
        ),
        "screen_photo": Noul(
            instructions="Is `photo` a picture of a screen, such as a phone or computer showing another photo, "
                         "rather than a photo taken directly?",
            criteria=NoulCriteria(
                true="A screen was photographed: its edge, glare, cracked glass, an app's bars or buttons, "
                     "or screen pixels show.",
                false="A photo taken directly of the scene.",
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
        # Only asked about unloading photos when the plant's location is given: see `questions_for`.
        **coordinate("lat", "latitude", "Lat or Latitude", 90),
        **coordinate("long", "longitude", "Long, Lng or Longitude", 180),
    },
    "bill": {
        "orientation": orientation("bill"),
        "weigh_bill": Noul(
            instructions="Is `bill` a weighbridge bill (a weighment slip)?",
            criteria=NoulCriteria(
                true="A printed slip from a weighbridge that records a vehicle's weight, such as gross, tare and net weight.",
                false="Any other document or photo, including a handwritten note or tally, or a receipt or invoice "
                      "that records no vehicle weight.",
            ),
        ),
        "slip_kind": Choice(
            instructions="If `bill` is a weighbridge slip, which weights are filled in on it?",
            criteria={
                "one_weight": "Only one weight, from the first weighing; the other weight and the net weight are blank or zero.",
                "both_weights": "Both weights (gross and tare, loaded and empty, or first and second), and the net weight.",
                NOT_SHOWN: "It isn't a weighbridge slip, or its weights can't be read.",
            },
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
        # Only asked when a quantity is typed: see `ask`.
        "net_weight": Choice(
            instructions=(
                "Is the net weight printed on `bill` the same as `quantity_kg` kilograms? The bill may label it "
                "Net Weight, Net Wt, Net or Difference. Compare the number of kilograms only, ignoring leading "
                "zeros, spaces, thousands separators and the unit. They are the same only if the numbers are equal."
            ),
            criteria={
                "different": "The bill shows a net weight, and it is a different number of kilograms from `quantity_kg`.",
                "same": "The bill shows a net weight of exactly `quantity_kg` kilograms.",
                NOT_SHOWN: "No net weight is printed on the bill, or it can't be read well enough to compare.",
            },
        ),
        "gross_vs_tare": Choice(
            instructions="If `bill` labels a gross (loaded) weight and a tare (empty) weight, which is the larger number?",
            criteria={
                "gross_heavier": "The weight labelled gross is larger than the weight labelled tare.",
                "tare_heavier": "The weight labelled tare is larger than the weight labelled gross.",
                NOT_SHOWN: "The bill doesn't label both a gross and a tare weight, or they can't be read.",
            },
        ),
        # Both layouts are asked about at once; `weighings` uses whichever the bill has.
        **bill_time("gross", GROSS),
        **bill_time("tare", TARE),
        **bill_time("in", TIME_IN),
        **bill_time("out", TIME_OUT),
        "first_weight": Choice(
            instructions="Which weight on `bill` was taken first, at the Time In (the first weighment)?",
            criteria={
                "tare": "The tare (empty) weight: the bill shows it with the Time In, or labels it the first weight.",
                "gross": "The gross (loaded) weight: the bill shows it with the Time In, or labels it the first weight.",
                NOT_SHOWN: "The bill has no Time In, or doesn't show which weight was taken first.",
            },
        ),
        # Whether the weighbridge's stamp is there, and whose name it shows, are asked apart: on 22 Oct 2026 one
        # question for both was unsure (0.45 to 0.79) on 2 of 3 clearly stamped slips.
        "weighbridge_seal": Noul(
            instructions="Apart from any stamp of `our_company`, is an ink stamp of the weighbridge pressed on `bill`?",
            criteria=NoulCriteria(
                true="A rubber stamp impression in ink, usually with the weighbridge's or its owner's name, even if "
                     "faint, tilted, upside down or partly over the printed text or a signature.",
                false="No ink stamp other than one of `our_company`. A printed logo or heading, a signature or "
                      "handwriting on its own doesn't count.",
            ),
        ),
        "weighbridge_name": Choice(
            instructions="If `bill` has an ink stamp of the weighbridge, apart from any stamp of `our_company`, does "
                         "the name in that stamp match the weighbridge's name printed in the slip's heading?",
            criteria={
                "different": "The stamp shows a business name that is not the one printed in the heading.",
                "same": "The stamp shows the same name as the heading, even if shortened or only partly legible.",
                NOT_SHOWN: "There is no weighbridge stamp, or the name in it can't be made out.",
            },
        ),
    },
}

# The questions that read where a photo was taken, in order: latitude, then longitude.
PLACE = ("lat_degrees", "lat_decimals", "long_degrees", "long_decimals")
# The questions about the weighbridge's stamp (seal) on a bill, asked only for a Verra project trip, and our company's
# name as on its own stamp, sent with them so that stamp isn't taken for the weighbridge's.
SEAL = ("weighbridge_seal", "weighbridge_name")
OUR_COMPANY = "Green Worms Eco Solutions LLP, West Hill, Calicut"

# Which readings to report for each slot, as (key, label, [question ids joined by the separator], separator).
REPORT = {
    "photo": [
        ("photo_time", "photo time", ["stamp_hour", "stamp_minute"], ":"),
        ("photo_date", "photo date", ["stamp_day", "stamp_month"], "/"),
    ],
    "bill": [
        ("gross_time", "gross time", ["gross_hour", "gross_minute", "gross_ampm"], ":"),
        ("gross_date", "gross date", ["gross_day", "gross_month"], "/"),
        ("tare_time", "tare time", ["tare_hour", "tare_minute", "tare_ampm"], ":"),
        ("tare_date", "tare date", ["tare_day", "tare_month"], "/"),
        ("in_time", "time in", ["in_hour", "in_minute", "in_ampm"], ":"),
        ("in_date", "date in", ["in_day", "in_month"], "/"),
        ("out_time", "time out", ["out_hour", "out_minute", "out_ampm"], ":"),
        ("out_date", "date out", ["out_day", "out_month"], "/"),
    ],
}

# What each yes/no answer means, as (check label, what passing shows, what failing shows, whether yes passes).
NOUL_CHECKS = {
    "shows_vehicle": ("Vehicle in photo", "Shows a vehicle", "No vehicle in the photo", True),
    "gps_stamp": ("GPS stamp", "GPS stamp printed on the photo", "No GPS stamp on the photo", True),
    "screen_photo": ("Taken directly", "A photo taken directly", "A photo of a screen", False),
    "weigh_bill": ("Weigh bill", "Is a weighbridge bill", "Not a weighbridge bill", True),
    "weighbridge_seal": ("Weighbridge stamp", "The weighbridge's own stamp is on it", "No weighbridge stamp", True),
}
# What each Choice answer means, as (check label, {option: (status, detail)}).
CHOICE_CHECKS = {
    "plate": ("Number plate", {
        "readable": ("pass", "Number plate can be read"),
        "partly_readable": ("review", "Number plate is only partly readable"),
        "not_visible": ("review", "No number plate in view"),
    }),
    "plate_number": ("Plate matches {number}", {
        "same": ("pass", "Plate shows {number}"),
        "different": ("fail", "Plate shows a different number"),
        "cannot_tell": ("review", "Plate can't be compared"),
    }),
    "slip_kind": ("Slip", {
        "both_weights": ("pass", "Final slip: both weights and the net"),
        "one_weight": ("pass", "First slip: one weight only"),
        NOT_SHOWN: ("review", "Its weights can't be read"),
    }),
    "bill_number": ("Bill matches {number}", {
        "same": ("pass", "Bill shows {number}"),
        "different": ("fail", "Bill shows a different vehicle number"),
        NOT_SHOWN: ("review", "No vehicle number readable on the bill"),
    }),
    "net_weight": ("Net weight is {quantity} kg", {
        "same": ("pass", "Bill shows a net weight of {quantity} kg"),
        "different": ("fail", "Bill shows a different net weight"),
        NOT_SHOWN: ("review", "No net weight readable on the bill"),
    }),
    "gross_vs_tare": ("Gross heavier than tare", {
        "gross_heavier": ("pass", "Gross is the larger weight"),
        "tare_heavier": ("fail", "Gross printed lighter than tare: the labels look swapped"),
        NOT_SHOWN: ("review", "Gross and tare labels can't be compared"),
    }),
    "weighbridge_name": ("Weighbridge stamp's name", {
        "same": ("pass", "The stamp's name matches the heading"),
        "different": ("fail", "Stamped by a different weighbridge from the one in the heading"),
        NOT_SHOWN: ("review", "The stamp's name can't be compared with the heading"),
    }),
}
# Bill questions that only count on the final slip: its net weight and labels, and the stamp proving it.
FINAL_ONLY = ("net_weight", "gross_vs_tare", *SEAL)

# A check's status is pass, fail (a problem found) or review (unsure). Nothing is rejected without a person.
TITLES = {"pass": "Accepted", "problem": "Flagged: problem found", "review": "Flagged: please check"}
GROUPS = {"loading": "Loading photos", "unloading": "Unloading photos", "bill": "Weigh bills",
          "times": "Times", "trips": "Other trips"}


def fit(img: PILImage.Image) -> PILImage.Image:
    """Scale the longest side to the API maximum (small text reads better larger), padding if too narrow."""
    scale = MAX_SIDE / max(img.size)
    img = img.resize((round(img.width * scale), round(img.height * scale)), PILImage.LANCZOS)
    if min(img.size) < MIN_SIDE:
        canvas = PILImage.new("RGB", (max(img.width, MIN_SIDE), max(img.height, MIN_SIDE)), "white")
        canvas.paste(img, ((canvas.width - img.width) // 2, (canvas.height - img.height) // 2))
        img = canvas
    return img


def questions_for(slot: str, quantity: str | None, place: bool = False, seal: bool = False) -> dict:
    """The questions asked about one image: the net weight only when a quantity was typed, where
    the photo was taken only with `place` (an unloading photo, when the plant's location is given),
    and the weighbridge's stamp only with `seal` (a bill of a Verra project trip).
    """
    return {q: v for q, v in QUESTIONS[slot].items()
            if (quantity or q != "net_weight") and (place or q not in PLACE) and (seal or q not in SEAL)}


def ask(client: TypeSafeClient, slot: str, img: PILImage.Image, vehicle_number: str, model: str,
        quantity: str | None = None, place: bool = False, seal: bool = False):
    state = {slot: Image.from_pil(fit(img), format="jpeg"), "vehicle_number": vehicle_number}
    if slot == "bill" and quantity:
        state["quantity_kg"] = quantity
    if seal:
        state["our_company"] = OUR_COMPANY
    return client.system_one(state=state, questions=questions_for(slot, quantity, place, seal), model=model)


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
        value = None if missing else sep.join(p["choice"] for p in parts[:2])
        if value and len(parts) == 3:  # a bill time: hour as printed, minute, and AM or PM
            value = to_24h(value, parts[2]["choice"])
        readings.append({
            "key": key,
            "label": label,
            "value": value,
            "missing": missing,
            "parts": parts,
            "min_confidence": min(p["confidence"] for p in parts),
        })
    if all(q in answers for q in PLACE):
        readings.append(position(answers))
    return readings


def position(answers: dict[str, dict]) -> dict:
    """The GPS stamp's latitude and longitude as a reading: "11.4404, 75.9419" when picked to four decimals
    (`read_line`), otherwise "11.44, 75.94" from the two-decimal questions. `coarse` is always the two-decimal one.

    The plant's location isn't in the questions, so these answers stay right whichever plant is checked.
    """
    parts = [{"id": q, **answers[q]} for q in PLACE]
    missing = any(p["choice"] == NOT_SHOWN for p in parts)
    c = [p["choice"] for p in parts]
    coarse = {"value": None if missing else f"{c[0]}.{c[1]}, {c[2]}.{c[3]}", "missing": missing,
              "min_confidence": min(p["confidence"] for p in parts)}
    reading = {"key": "position", "label": "photo position", "parts": parts, "coarse": coarse, **coarse}
    picks = [{"id": q, **answers[q]} for q in PICKS if q in answers]
    if len(picks) == 2 and all(p["choice"] != NOT_SHOWN for p in picks):
        reading |= {"value": ", ".join(p["choice"] for p in picks), "missing": False, "parts": picks,
                    "min_confidence": min(p["confidence"] for p in picks)}
    return reading


def to_24h(hhmm: str, ampm: str) -> str:
    """'03:06' PM -> '15:06', '12:30' AM -> '00:30'. An hour above 12 is already on a 24-hour clock."""
    hour, minute = hhmm.split(":")
    h = int(hour)
    if ampm == "pm" and h < 12:
        h += 12
    elif ampm == "am" and h == 12:
        h = 0
    return f"{h:02d}:{minute}"


def noul_status(yes: float, pass_when_yes: bool = True, qid: str | None = None) -> str:
    """pass, fail or review for a yes/no answer, by the question's own thresholds or YES and NO."""
    high, low = NOUL_THRESHOLDS.get(qid, (YES, NO))
    status = "pass" if yes >= high else "fail" if yes <= low else "review"
    return status if pass_when_yes or status == "review" else {"pass": "fail", "fail": "pass"}[status]


def checks_for(slot: str, answers: dict[str, dict], vehicle_number: str, quantity: str | None = None) -> list[dict]:
    """Turn one image's yes/no and Choice answers into pass, fail or review checks, shown with the image.

    `certainty` is the probability of yes for a yes/no answer (`measure` "yes"), and the Choice
    confidence otherwise. A question that wasn't asked (net weight with no quantity) gives no check,
    and nor do the final-slip questions on a first slip. The trip's verdict combines these answers
    across images in `trip_checks`.
    """
    first_slip = answers.get("slip_kind", {}).get("choice") == "one_weight"
    checks = []
    for qid in QUESTIONS[slot]:
        if qid not in answers or (first_slip and qid in FINAL_ONLY):
            continue
        a = answers[qid]
        if qid in NOUL_CHECKS:
            label, yes, no, pass_when_yes = NOUL_CHECKS[qid]
            status = noul_status(a["yes"], pass_when_yes, qid)
            detail = {"pass": yes, "fail": no, "review": f"Unsure: {(yes if pass_when_yes else no).lower()}?"}[status]
            checks.append({"key": qid, "label": label, "status": status, "detail": detail,
                           "certainty": a["yes"], "measure": "yes"})
        elif qid in CHOICE_CHECKS:
            label, outcomes = CHOICE_CHECKS[qid]
            status, detail = outcomes[a["choice"]]
            if a["confidence"] < CONFIDENT:
                status, detail = "review", f"Unsure: {detail[0].lower()}{detail[1:]}"
            typed = {"number": vehicle_number, "quantity": quantity}
            checks.append({"key": qid, "label": label.format(**typed), "status": status,
                           "detail": detail.format(**typed), "certainty": a["confidence"],
                           "measure": "confidence"})
    return checks


YEAR = 2024  # only the day and month are read; a leap year, so 29/02 is a date


def moment(time: dict, date: dict) -> dict | None:
    """A time and a date reading as {"at": datetime, "time": "08:57", "date": "15/10", "certainty": ...}.

    None if either is missing or the date doesn't exist, such as 31/02.
    """
    if time["missing"] or date["missing"]:
        return None
    (hour, minute), (day, month) = map(int, time["value"].split(":")), map(int, date["value"].split("/"))
    try:
        at = datetime(YEAR, month, day, hour, minute)
    except ValueError:
        return None
    return {"at": at, "time": time["value"], "date": date["value"],
            "certainty": min(time["min_confidence"], date["min_confidence"])}


def minutes_between(a: dict, b: dict) -> int:
    """Minutes from moment `a` to moment `b`, negative if `b` is earlier. Runs across New Year when that's nearer."""
    gap = round((b["at"] - a["at"]).total_seconds() / 60)
    half_year = 183 * 24 * 60
    return gap - 2 * half_year if gap > half_year else gap + 2 * half_year if gap < -half_year else gap


def duration(minutes: int) -> str:
    """75 -> '1 h 15 min', 60 -> '1 h', 13 -> '13 min'."""
    hours, minutes = divmod(minutes, 60)
    return " ".join(part for part in (f"{hours} h" if hours else "", f"{minutes} min" if minutes or not hours else "") if part)


def show(*moments: dict) -> list[str]:
    """Moments as text for a check's detail: times only, with dates when they're not all the same day."""
    one_day = len({m["date"] for m in moments}) == 1
    return [m["time"] if one_day else f"{m['time']} on {m['date']}" for m in moments]


def weighings(bill: dict) -> dict | None:
    """When the gross and tare weights were taken: {"gross": moment, "tare": moment, "certainty", "source"}.

    A bill prints a date and time with each weight, or only a Time In and Time Out, with `first_weight`
    saying which weight was taken at Time In. If both layouts can be read, the surer one is used.
    None if neither can be read.
    """
    r = {x["key"]: x for x in bill["readings"]}
    found = []
    gross, tare = moment(r["gross_time"], r["gross_date"]), moment(r["tare_time"], r["tare_date"])
    if gross and tare:
        found.append({"gross": gross, "tare": tare, "certainty": min(gross["certainty"], tare["certainty"]),
                      "source": "the times printed with each weight"})
    first = bill["answers"]["first_weight"]
    time_in, time_out = moment(r["in_time"], r["in_date"]), moment(r["out_time"], r["out_date"])
    if time_in and time_out and first["choice"] != NOT_SHOWN:
        gross, tare = (time_in, time_out) if first["choice"] == "gross" else (time_out, time_in)
        found.append({"gross": gross, "tare": tare,
                      "certainty": min(time_in["certainty"], time_out["certainty"], first["confidence"]),
                      "source": f"Time In and Time Out, {first['choice']} first"})
    return max(found, key=lambda w: w["certainty"], default=None)


def check(group: str, key: str, label: str, status: str, detail: str, certainty: float | None = None) -> dict:
    return {"group": group, "key": key, "label": label, "status": status, "detail": detail,
            "certainty": certainty, "measure": "confidence" if certainty is not None else None}


def timing_check(group: str, key: str, label: str, passed: bool, yes: str, no: str, certainty: float) -> dict:
    """A check worked out in code from times Jev read: unsure readings go to a person."""
    status = "review" if certainty < CONFIDENT else "pass" if passed else "fail"
    return check(group, key, label, status, (yes if passed else no) + (" (unsure reading)" if status == "review" else ""),
                 certainty)


def names(results: list[dict]) -> str:
    return ", ".join(r["name"] for r in results)


def each_photo(role: str, key: str, label: str, results: list[dict], qid: str) -> dict:
    """A yes/no question that every photo of a role must pass, such as "shows a vehicle"."""
    _, yes, no, pass_when_yes = NOUL_CHECKS[qid]
    statuses = {r["name"]: noul_status(r["answers"][qid]["yes"], pass_when_yes, qid) for r in results}
    failed = [r for r in results if statuses[r["name"]] == "fail"]
    unsure = [r for r in results if statuses[r["name"]] == "review"]
    if failed:
        return check(role, key, label, "fail", f"{no}: {names(failed)}")
    if unsure:
        return check(role, key, label, "review", f"Unsure for {names(unsure)}")
    return check(role, key, label, "pass", f"{len(results)} of {len(results)} photos")


def plate_check(role: str, results: list[dict], vehicle_number: str) -> dict:
    """At least one photo of the role clearly shows the typed number; any photo clearly showing another is a problem."""
    label = f"Plate matches {vehicle_number}"
    sure = [r for r in results if r["answers"]["plate_number"]["confidence"] >= CONFIDENT]
    different = [r for r in sure if r["answers"]["plate_number"]["choice"] == "different"]
    same = [r for r in sure if r["answers"]["plate_number"]["choice"] == "same"]
    if different:
        return check(role, f"{role}_plate", label, "fail", f"A different number in {names(different)}")
    if same:
        return check(role, f"{role}_plate", label, "pass", f"Seen clearly in {len(same)} of {len(results)}",
                     max(r["answers"]["plate_number"]["confidence"] for r in same))
    return check(role, f"{role}_plate", label, "review", "No photo shows the plate clearly enough to compare")


def photo_checks(role: str, results: list[dict], vehicle_number: str) -> list[dict]:
    if not results:
        return [check(role, f"{role}_missing", GROUPS[role], "review", "None added")]
    return [
        each_photo(role, f"{role}_vehicle", "Vehicle in photo", results, "shows_vehicle"),
        each_photo(role, f"{role}_stamp", "GPS stamp", results, "gps_stamp"),
        each_photo(role, f"{role}_direct", "Taken directly", results, "screen_photo"),
        plate_check(role, results, vehicle_number),
    ]


def metres_between(a: tuple[float, float], b: tuple[float, float]) -> float:
    """The distance between two (latitude, longitude) points over the earth's surface, in metres (haversine)."""
    (lat1, lon1), (lat2, lon2) = (map(math.radians, p) for p in (a, b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


def distance(metres: float) -> str:
    """40 -> '40 m', 1234 -> '1.2 km', 38210 -> '38 km'."""
    return f"{round(metres)} m" if metres < 1000 else f"{metres / 1000:.1f} km" if metres < 10_000 else f"{metres / 1000:.0f} km"


def from_square(value: str, plant: tuple[float, float]) -> float:
    """How far the plant is from the square a position reading such as "11.44, 75.94" stands for, in metres.

    The stamp's later digits are cut off, so "11.44, 75.94" is anywhere from 11.44 to 11.45 and 75.94 to
    75.95, a square about 1.1 km across, and "11.4404, 75.9419" a square about 11 m across. 0 when the
    plant is in the square.
    """
    corner = [float(x) for x in value.split(", ")]
    size = 10.0 ** -len(value.split(", ")[0].partition(".")[2])
    nearest = [min(max(at, low), low + size) for at, low in zip(plant, corner)]
    return metres_between(plant, tuple(nearest))


def chance_away(answers: dict[str, dict], plant: tuple[float, float], radius: float) -> float:
    """The probability that a photo's two-decimal reading places it farther than `radius` from the plant,
    over every combination of the likeliest answers (each place question's top five).

    An unsure digit can still be clear about the plant: 11.16 or 11.11 are both far from 11.44.
    """
    away = 0.0
    for combo in itertools.product(*(answers[q]["top"] for q in PLACE)):
        chosen = [option for option, _ in combo]
        if NOT_SHOWN not in chosen and from_square(f"{chosen[0]}.{chosen[1]}, {chosen[2]}.{chosen[3]}", plant) > radius:
            away += math.prod(p for _, p in combo)
    return away


def plant_check(results: list[dict], plant: tuple[float, float], limits: Limits) -> dict:
    """At least one unloading photo stamped within `plant_radius` of the plant; any stamped farther is a problem.

    Within the radius takes a position picked surely to four decimals (a square about 11 m across), so the
    distance is known to the metre. Away can also be shown by the two-decimal reading: when even the
    nearest point of its 1.1 km square (`from_square`) is past the radius, read surely or by `chance_away`.
    But a two-decimal "away" against another photo surely near asks a person: on 19 Oct 2026 two photos
    at the plant were read 11.40 or 11.14 for 11.44, 84% sure. Like the plate, one photo is enough to
    pass, and anything less sure asks a person.
    """
    radius, label = limits.plant_radius, "At the plant"
    near, away, unsure, unread = [], [], [], []  # near and away: (result, detail, certainty, metres, to four decimals)
    for r in results:
        p = next((x for x in r["readings"] if x["key"] == "position"), None)
        if not p:
            unread.append(r)
            continue
        if not p["missing"] and p["value"] != p["coarse"]["value"] and p["min_confidence"] >= CONFIDENT:
            m = from_square(p["value"], plant)
            (near if m <= radius else away).append((r, f"at {p['value']}, {distance(m)} from the plant", p["min_confidence"], m, True))
            continue
        c = p["coarse"]
        if not c["missing"] and c["min_confidence"] >= CONFIDENT:
            if (m := from_square(c["value"], plant)) > radius:
                away.append((r, f"at {c['value']}, at least {distance(m)} from the plant", c["min_confidence"], m, False))
                continue
        elif (no := chance_away(r["answers"], plant, radius)) >= YES:
            read = f"read as {c['value']}" if c["value"] else "partly read"
            away.append((r, f"away from the plant ({no:.0%} likely, {read})", no, None, False))
            continue
        (unread if c["missing"] else unsure).append(r)
    closest = min(near, key=lambda x: x[3], default=None)
    if away and (any(x[4] for x in away) or not near):
        r, detail, c, _, _ = next((x for x in away if x[4]), away[0])
        return check("unloading", "unloading_plant", label, "fail",
                     f"{r['name']} is stamped {detail} (within {distance(radius)} allowed)", c)
    if away:
        (r, detail, _, _, _), (n, there, c, _, _) = away[0], closest
        return check("unloading", "unloading_plant", label, "review",
                     f"{r['name']} seems stamped {detail}, but {n['name']} is {there}", c)
    if near:
        r, detail, c, _, _ = closest
        return check("unloading", "unloading_plant", label, "pass",
                     f"{len(near)} of {len(results)} stamped within {distance(radius)}: {r['name']} {detail}", c)
    if unsure:
        return check("unloading", "unloading_plant", label, "review",
                     f"Position not read surely enough to the metre on {names(unsure)}")
    return check("unloading", "unloading_plant", label, "review", f"GPS stamp position can't be read on {names(unread)}")


def final_slip(bills: list[dict]) -> dict | None:
    """The slip with both weights and the net, the one the times and net weight come from."""
    slips = [b for b in bills if b["answers"]["weigh_bill"]["yes"] >= 0.5]
    finals = [b for b in slips if b["answers"]["slip_kind"]["choice"] == "both_weights"]
    return max(finals, key=lambda b: b["answers"]["slip_kind"]["confidence"], default=None)


def choice_check(group: str, key: str, qid: str, answer: dict, vehicle_number: str, quantity: str | None, prefix: str = "") -> dict:
    label, outcomes = CHOICE_CHECKS[qid]
    status, detail = outcomes[answer["choice"]]
    if answer["confidence"] < CONFIDENT:
        status, detail = "review", f"Unsure: {detail[0].lower()}{detail[1:]}"
    typed = {"number": vehicle_number, "quantity": quantity}
    return check(group, key, label.format(**typed), status, prefix + detail.format(**typed), answer["confidence"])


def seal_checks(final: dict | None) -> list[dict]:
    """The final slip carries the weighbridge's own stamp.

    It passes once it is surely there, unless its name is surely another
    weighbridge's: like gross and tare, an unsure name would send most trips to a person for nothing.
    """
    if not final:
        return [check("bill", "weighbridge_seal", "Weighbridge stamp", "review", "No final slip to look for it on")]
    a, at, checks = final["answers"], f"{final['name']}: ", []
    theirs, name = a["weighbridge_seal"]["yes"], a["weighbridge_name"]
    status = noul_status(theirs, qid="weighbridge_seal")
    if status == "pass" and name["choice"] == "different" and name["confidence"] >= CONFIDENT:
        checks.append(choice_check("bill", "weighbridge_seal", "weighbridge_name", name, "", None, at))
    elif status == "pass":
        matched = name["choice"] == "same" and name["confidence"] >= CONFIDENT
        checks.append(check("bill", "weighbridge_seal", "Weighbridge stamp", "pass",
                            at + "The weighbridge's own stamp is on it" + ("" if matched else ", its name not surely compared"),
                            theirs))
    else:
        detail = {"fail": "No weighbridge stamp", "review": "Unsure: is the weighbridge's own stamp on it?"}[status]
        checks.append(check("bill", "weighbridge_seal", "Weighbridge stamp", status, at + detail, theirs))
    return checks


def bill_checks(bills: list[dict], vehicle_number: str, quantity: str | None, verra: bool = False) -> list[dict]:
    """A weighbridge slip, the final slip, the vehicle number on every slip, the final slip's net weight and labels,
    and for a Verra project trip, its stamp.
    """
    if not bills:
        return [check("bill", "bill_missing", "Weigh bills", "review", "None added")]
    slips = [b for b in bills if b["answers"]["weigh_bill"]["yes"] >= 0.5]
    ignored = [b for b in bills if b not in slips]
    note = f"; ignored as not a slip: {names(ignored)}" if ignored else ""
    final = final_slip(bills)
    checks = []
    if not any(b["answers"]["weigh_bill"]["yes"] >= YES for b in slips):
        checks.append(check("bill", "bill_found", "Weighbridge slip", "review", f"No image is clearly a weighbridge slip{note}"))
    elif final and final["answers"]["slip_kind"]["confidence"] >= CONFIDENT:
        checks.append(check("bill", "bill_found", "Final slip", "pass", f"{final['name']}: both weights and the net{note}",
                            final["answers"]["slip_kind"]["confidence"]))
    else:
        checks.append(check("bill", "bill_found", "Final slip", "review",
                            f"No slip clearly shows both weights and the net{note}"))

    sure = [b for b in slips if b["answers"]["bill_number"]["confidence"] >= CONFIDENT]
    different = [b for b in sure if b["answers"]["bill_number"]["choice"] == "different"]
    unclear = [b for b in slips if b not in sure or b["answers"]["bill_number"]["choice"] == NOT_SHOWN]
    label = f"Bills match {vehicle_number}"
    if different:
        checks.append(check("bill", "bill_number", label, "fail", f"A different vehicle number on {names(different)}"))
    elif unclear or not slips:
        checks.append(check("bill", "bill_number", label, "review", f"Can't be compared on {names(unclear) or 'any slip'}"))
    else:
        shown = f"All {len(slips)} slips show" if len(slips) > 1 else "The slip shows"
        checks.append(check("bill", "bill_number", label, "pass", f"{shown} {vehicle_number}",
                            min(b["answers"]["bill_number"]["confidence"] for b in slips)))

    if not quantity:
        checks.append(check("bill", "net_weight", "Net weight", "review", "Quantity not typed"))
    elif not final:
        checks.append(check("bill", "net_weight", f"Net weight is {quantity} kg", "review", "No final slip to read it from"))
    else:
        checks.append(choice_check("bill", "net_weight", "net_weight", final["answers"]["net_weight"], vehicle_number, quantity,
                                   f"{final['name']}: "))
    # Only when Jev is sure either way: many slips don't label gross and tare (19 Oct 2026 run), and an
    # unsure answer there would send most trips to a person for nothing.
    if final and final["answers"]["gross_vs_tare"]["confidence"] >= CONFIDENT \
            and final["answers"]["gross_vs_tare"]["choice"] != NOT_SHOWN:
        checks.append(choice_check("bill", "gross_vs_tare", "gross_vs_tare", final["answers"]["gross_vs_tare"],
                                   vehicle_number, quantity, f"{final['name']}: "))
    return checks + (seal_checks(final) if verra else [])


def photo_moments(results: list[dict]) -> list[tuple[dict, dict | None]]:
    """Each photo with its GPS stamp time as a moment, or None if it can't be read."""
    out = []
    for r in results:
        rd = {x["key"]: x for x in r["readings"]}
        out.append((r, moment(rd["photo_time"], rd["photo_date"])))
    return out


def photo_window(role: str, tare_first: bool) -> tuple[str | None, str | None]:
    """Which weighings a loading or unloading photo must fall between: (after, before), None where open.

    Tare first, loading is between tare and gross and unloading after gross. Gross first, loading is
    before gross and unloading between gross and tare.
    """
    if role == "loading":
        return ("tare", "gross") if tare_first else (None, "gross")
    return ("gross", None) if tare_first else ("gross", "tare")


def photo_timing(role: str, results: list[dict], w: dict, limits: Limits, tare_first: bool = True) -> dict | None:
    """Every loading or unloading photo in its window of the weighings (`photo_window`), give or take the clock slack."""
    if not results:
        return None
    slack = limits.clock_slack
    allowing = f", allowing {duration(slack)} clock difference" if slack else ""
    after, before = photo_window(role, tare_first)
    shown = dict(zip(("tare", "gross"), show(w["tare"], w["gross"])))
    label = (f"Between {after} and {before}" if after and before else f"After {after}" if after else f"Before {before}")
    taken = photo_moments(results)
    unread = [r for r, m in taken if not m]
    wrong, unsure, times = [], [], []
    for r, m in taken:
        if not m:
            continue
        early = bool(after) and minutes_between(w[after], m) < -slack
        late = bool(before) and minutes_between(m, w[before]) < -slack
        certainty = min(m["certainty"], w["certainty"])
        (unsure if certainty < CONFIDENT else wrong if early or late else times).append((r, m, early))
    word = "Loading" if role == "loading" else "Unloading"
    if wrong:
        r, m, early = wrong[0]
        p = show(m, w["tare"], w["gross"])[0]
        where = f"before {after} {shown[after]}" if early else f"after {before} {shown[before]}"
        return check(role, f"{role}_timing", label, "fail", f"{word} {p} ({r['name']}) is {where}{allowing}",
                     min(m["certainty"], w["certainty"]))
    if unread:
        return check(role, f"{role}_timing", label, "review", f"GPS stamp time can't be read on {names(unread)}")
    if unsure:
        return check(role, f"{role}_timing", label, "review", f"Unsure reading of {names([r for r, *_ in unsure])}")
    when = ", ".join(sorted({show(m, w["tare"], w["gross"])[0] for _, m, _ in times}))
    window = (f"between {after} {shown[after]} and {before} {shown[before]}" if after and before
              else f"after {after} {shown[after]}" if after else f"before {before} {shown[before]}")
    return check(role, f"{role}_timing", label, "pass", f"{word} {when} is {window}",
                 min(min(m["certainty"] for _, m, _ in times), w["certainty"]))


def same_day(results: list[dict], w: dict) -> dict | None:
    """Every unloading photo dated the same day as the gross weighing (by the calendar date, so a trip
    weighed just before midnight and unloaded just after is flagged too)."""
    if not results:
        return None
    gross = w["gross"]
    label = "Same day as gross"
    taken = photo_moments(results)
    unread = [r for r, m in taken if not m]
    other_day = [(r, m) for r, m in taken if m and m["date"] != gross["date"]]
    sure = [(r, m) for r, m in other_day if min(m["certainty"], w["certainty"]) >= CONFIDENT]
    if sure:
        r, m = sure[0]
        return check("unloading", "unloading_day", label, "fail",
                     f"Unloading on {m['date']} ({r['name']}), gross on {gross['date']}", min(m["certainty"], w["certainty"]))
    if other_day:
        return check("unloading", "unloading_day", label, "review",
                     f"Unsure reading: {names([r for r, _ in other_day])} dated {other_day[0][1]['date']}, gross on {gross['date']}")
    if unread:
        return check("unloading", "unloading_day", label, "review", f"GPS stamp date can't be read on {names(unread)}")
    return check("unloading", "unloading_day", label, "pass", f"All on {gross['date']}",
                 min(min(m["certainty"] for _, m in taken), w["certainty"]))


def trip_checks(images: dict[str, list[dict]], vehicle_number: str, quantity: str | None = None,
                limits: Limits = Limits(), plant: tuple[float, float] | None = None,
                verra: bool = False) -> tuple[list[dict], dict | None]:
    """Every check for a trip, and the weighings they used (None if the times couldn't be read).

    Tare first, the trip runs tare -> loading -> gross -> unloading: the weighings must be at least
    `min_gap_tare_first` apart, loading photos fall between them and unloading photos after gross.
    Gross first, it runs loading -> gross -> unloading -> tare: the weighings must be at least
    `min_gap_gross_first` apart, loading photos fall before gross and unloading photos between them.
    Photos may be off by `clock_slack`, and unloading must be on the same day as the gross weighing.
    With `plant`, unloading must be stamped within `plant_radius` of it. With `verra` (a Verra project
    trip), the final slip must carry the weighbridge's stamp.
    """
    checks = photo_checks("loading", images.get("loading", []), vehicle_number)
    checks += photo_checks("unloading", images.get("unloading", []), vehicle_number)
    if plant and images.get("unloading"):
        checks.append(plant_check(images["unloading"], plant, limits))
    bills = images.get("bill", [])
    checks += bill_checks(bills, vehicle_number, quantity, verra)

    final = final_slip(bills)
    w = weighings(final) if final else None
    gap = minutes_between(w["tare"], w["gross"]) if w else None  # positive when the tare was taken first
    if not bills:
        return checks, None
    if not w:
        checks.append(check("times", "weigh_order", "Weighing order", "review",
                            "Tare and gross times can't be read from the final slip"))
        return checks, None
    g, t = show(w["gross"], w["tare"])
    if gap == 0:
        checks.append(check("times", "weigh_order", "Weighing order", "review", f"Tare and gross both at {g}"))
        return checks, w
    tare_first = gap > 0
    order = f"Tare first ({t}), then gross ({g})" if tare_first else f"Gross first ({g}), then tare ({t})"
    checks.append(timing_check("times", "weigh_order", "Weighing order", True, f"{order}, from {w['source']}", "",
                               w["certainty"]))
    if tare_first:
        least = limits.min_gap_tare_first
        checks.append(timing_check("times", "weigh_gap", "Tare to gross", gap >= least,
                                   f"{duration(gap)} apart (at least {duration(least)})",
                                   f"Only {duration(gap)} apart (at least {duration(least)} needed)", w["certainty"]))
    else:
        least = limits.min_gap_gross_first
        checks.append(timing_check("times", "weigh_gap", "Gross to tare", -gap >= least,
                                   f"{duration(-gap)} apart (at least {duration(least)})",
                                   f"Only {duration(-gap)} apart (at least {duration(least)} needed)", w["certainty"]))
    for role in ("loading", "unloading"):
        if c := photo_timing(role, images.get(role, []), w, limits, tare_first):
            checks.append(c)
    if c := same_day(images.get("unloading", []), w):
        checks.append(c)
    return checks, w


def timeline(images: dict[str, list[dict]], w: dict | None, checks: list[dict]) -> list[dict]:
    """The trip's events in time order, for the page: tare, loading, gross and unloading, marking those in a failed check."""
    failed = {c["key"] for c in checks if c["status"] == "fail"}
    events = []
    if w:
        events += [{"at": w["tare"], "what": "Tare", "bad": "weigh_order" in failed or "weigh_gap" in failed},
                   {"at": w["gross"], "what": "Gross", "bad": "weigh_order" in failed or "weigh_gap" in failed}]
    for role, word in (("loading", "Loading photos"), ("unloading", "Unloading photos")):
        times = [m for _, m in photo_moments(images.get(role, [])) if m]
        if times:
            first = min(times, key=lambda m: m["at"])
            events.append({"at": first, "what": word, "bad": f"{role}_timing" in failed or f"{role}_day" in failed})
    events.sort(key=lambda e: e["at"]["at"])
    return [{"time": e["at"]["time"], "date": e["at"]["date"], "what": e["what"], "bad": e["bad"]} for e in events]


def decide(checks: list[dict]) -> dict:
    """The verdict from a list of checks: any failed check flags a problem, any unsure one asks a person.

    Nothing is rejected here. `flags` lists the checks that didn't pass, problems first.
    """
    statuses = {c["status"] for c in checks}
    status = "problem" if "fail" in statuses else "review" if "review" in statuses else "pass"
    flags = sorted((c for c in checks if c["status"] != "pass"), key=lambda c: c["status"] != "fail")
    return {"status": status, "title": TITLES[status], "checks": checks, "flags": flags}


def verdict(images: dict[str, list[dict]], vehicle_number: str, quantity: str | None = None,
            limits: Limits = Limits(), extra: list[dict] = (), plant: tuple[float, float] | None = None,
            verra: bool = False) -> dict:
    """One verdict for the trip, with its checks, flags and timeline. `extra` are checks across trips."""
    checks, w = trip_checks(images, vehicle_number, quantity, limits, plant, verra)
    checks += list(extra)
    # Between the two weighings, for finding a vehicle on two trips at once: only when their order is clear.
    clear = any(c["key"] == "weigh_order" and c["status"] == "pass" for c in checks)
    window = sorted([w["tare"]["at"], w["gross"]["at"]]) if w and clear else None
    return {**decide(checks), "timeline": timeline(images, w, checks),
            "window": [t.isoformat() for t in window] if window else None}


@dataclass
class Upload:
    """One image of a trip, as uploaded: its file name and bytes."""

    name: str
    data: bytes


def role_of(name: str) -> str | None:
    """What an image in a trip folder is for, by its file name: collection1.jpg is a loading photo."""
    stem = name.lower()
    if not stem.endswith(IMAGE_TYPES):
        return None
    for prefix, role in (("collection", "loading"), ("loading", "loading"), ("unloading", "unloading"),
                         ("weighbill", "bill"), ("weigh_bill", "bill"), ("bill", "bill")):
        if stem.startswith(prefix):
            return role
    return None


def read_trip_folder(folder: Path) -> dict[str, list[Upload]]:
    """The images in a trip folder by role, in file-name order."""
    found = {role: [] for role in ROLES}
    for path in sorted(folder.iterdir()):
        if path.is_file() and (role := role_of(path.name)):
            found[role].append(Upload(path.name, path.read_bytes()))
    return found


def load_bytes(data: bytes) -> PILImage.Image:
    with PILImage.open(io.BytesIO(data)) as original:
        return ImageOps.exif_transpose(original).convert("RGB")


def load(path: Path) -> PILImage.Image:
    return load_bytes(path.read_bytes())


def fingerprints(data: bytes, img: PILImage.Image | None = None) -> dict:
    """An exact fingerprint (md5 of the file) and a look-alike one (a 64-bit difference hash of the picture)."""
    img = img or load_bytes(data)
    px = img.convert("L").resize((9, 8), PILImage.LANCZOS).tobytes()
    bits = sum(1 << i for i in range(64) if px[(i // 8) * 9 + i % 8] > px[(i // 8) * 9 + i % 8 + 1])
    return {"md5": hashlib.md5(data).hexdigest(), "dhash": f"{bits:016x}"}


LOOKS_ALIKE = 3  # at most this many of the 64 look-alike bits differ: a re-saved or resized copy
FEATURELESS = 6  # a look-alike fingerprint with this few bits set, or unset, is a plain image that says nothing


class ImageIndex:
    """Fingerprints of every image in known trips, to find an image used again in another trip."""

    def __init__(self):
        self.items: list[tuple[str, str, str, int]] = []  # (trip, image name, md5, dhash)

    def add(self, trip: str, name: str, prints: dict) -> None:
        self.items.append((trip, name, prints["md5"], int(prints["dhash"], 16)))

    @classmethod
    def of_folders(cls, root: Path) -> "ImageIndex":
        """Index every trip folder under `root`: a folder holding role-named images."""
        index = cls()
        for folder in sorted({p.parent for p in root.rglob("*") if p.is_file() and role_of(p.name)}):
            trip = folder.relative_to(root).as_posix()
            for uploads in read_trip_folder(folder).values():
                for u in uploads:
                    try:
                        index.add(trip, u.name, fingerprints(u.data))
                    except OSError:
                        continue
        return index

    def matches(self, prints: dict, trip: str | None) -> list[dict]:
        """Images in other trips that are this image: the same file, or one that looks the same."""
        dhash = int(prints["dhash"], 16)
        found = []
        for other, name, md5, h in self.items:
            if other == trip:
                continue
            if md5 == prints["md5"]:
                found.append({"trip": other, "name": name, "exact": True})
            elif FEATURELESS < bin(dhash).count("1") < 64 - FEATURELESS and bin(h ^ dhash).count("1") <= LOOKS_ALIKE:
                found.append({"trip": other, "name": name, "exact": False})
        return found


def short_trip(trip: str) -> str:
    """A trip id for people: its folder's name, such as 171562 for day/InwardLog/171562."""
    return trip.rsplit("/", 1)[-1]


def and_list(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def reuse_check(images: dict[str, list[dict]], index: ImageIndex, trip: str | None) -> dict:
    """Any image of this trip already used in another trip is a problem; a look-alike asks a person.

    The detail groups this trip's images by the trips they were seen in, such as
    "collection1.jpg and collection2.jpg are also in trip 171562".
    """
    seen = {True: {}, False: {}}  # exact or look-alike -> {image name: [other trips]}
    for results in images.values():
        for r in results:
            for m in index.matches(r["prints"], trip):
                r.setdefault("used_in", []).append(m)
                others = seen[m["exact"]].setdefault(r["name"], [])
                if short_trip(m["trip"]) not in others:
                    others.append(short_trip(m["trip"]))

    def describe(found: dict) -> str:
        groups = {}
        for name, others in found.items():
            groups.setdefault(tuple(sorted(others)), []).append(name)
        return "; ".join(f"{and_list(names)} {'is' if len(names) == 1 else 'are'} also in trip{'s' if len(others) > 1 else ''} "
                         f"{and_list(list(others))}" for others, names in groups.items())

    label = "Images not used in other trips"
    if seen[True]:
        return check("trips", "reused", label, "fail", describe(seen[True]))
    if seen[False]:
        return check("trips", "reused", label, "review", "Looks the same: " + describe(seen[False]))
    return check("trips", "reused", label, "pass", "None seen in another trip")


def overlap_checks(trips: dict[str, dict]) -> dict[str, dict]:
    """A vehicle can't be on two trips at once: {trip: check} for each trip whose window between weighings overlaps
    another trip's with the same vehicle number. `trips` maps a trip id to {"vehicle_number", "verdict"}.
    """
    windows = {}
    for trip, t in trips.items():
        if w := t["verdict"].get("window"):
            windows[trip] = (t["vehicle_number"], datetime.fromisoformat(w[0]), datetime.fromisoformat(w[1]))
    found = {}
    for a, (va, sa, ea) in windows.items():
        for b, (vb, sb, eb) in windows.items():
            if a < b and va == vb and sa < eb and sb < ea:
                for x, y in ((a, b), (b, a)):
                    found.setdefault(x, []).append(y)
    return {trip: check("trips", "overlap", "One trip at a time", "fail",
                        f"{trips[trip]['vehicle_number']} is also on trip {', '.join(others)} at the same time")
            for trip, others in found.items()}


def add_checks(trip: dict, extra: list[dict]) -> None:
    """Add checks across trips to a finished trip's verdict, and decide again."""
    v = trip["verdict"]
    trip["verdict"] = {**v, **decide(v["checks"] + extra)}


def question_version(slot: str, place: bool = False, seal: bool = False) -> str:
    """Changes whenever a question changes, so saved answers from older questions aren't reused.

    The place and stamp questions only count with `place` and `seal`, so adding them kept the answers
    saved before. With the stamp questions, our company's name counts too, as it is sent with them.
    """
    asked = {q: v for q, v in QUESTIONS[slot].items() if (place or q not in PLACE) and (seal or q not in SEAL)}
    if place:  # and how the line is found and picked from
        asked |= {"line": LINE, "pick": pick("{axis}", "{labels}", ["{start}", "{other}"])}
    text = json.dumps({q: v.model_dump(mode="json") for q, v in asked.items()}, sort_keys=True) + (BOX_MODEL if place else "") \
        + (OUR_COMPANY if seal else "")
    return hashlib.md5(text.encode()).hexdigest()[:10]


TURNING = "turn3"  # how images are turned before the answers are kept; changing it means asking again


def answer_key(prints: dict, slot: str, vehicle_number: str, quantity: str | None, model: str, place: bool = False,
               seal: bool = False) -> str:
    """What an image's answers depend on, to save them and reuse them: the image, the questions, what was typed, the model.

    Not the plant's location: it isn't sent, so the same answers serve any plant.
    """
    return "|".join([prints["md5"], slot, question_version(slot, place, seal), TURNING, vehicle_number, quantity or "", model])


def result_from(slot: str, raw: dict, vehicle_number: str, quantity: str | None) -> dict:
    """An image's result from Jev's raw answers: its checks and readings are worked out here, so changing a
    threshold or rule needs no new request.
    """
    tokens = raw["input_tokens"]
    return {**raw, "slot": slot, "checks": checks_for(slot, raw["answers"], vehicle_number, quantity),
            "readings": readings_for(slot, raw["answers"]), "cost_usd": tokens * PRICE_PER_INPUT_TOKEN}


def turn_for(answer, first: bool) -> int:
    """How far to turn an image (counterclockwise) by its `orientation` answer, or 0 to keep it.

    A confident answer is followed. On the first look, an image surely sideways but unsure which way is
    turned the likelier way. Which way is close to a coin flip for Jev, so the next look decides only
    between the two places that turn can have left it: upright, or upside down (then half a turn more).
    In the 19 Oct 2026 run, 6 of 35 slips turned once were left upside down by waiting for ROTATE there.
    """
    p = answer.probabilities
    if not first:
        return 180 if p.get("upside_down", 0) > p.get("upright", 0) else 0
    if answer.choice != "upright" and answer.confidence >= ROTATE:
        return ROTATION[answer.choice]
    if p.get("top_points_left", 0) + p.get("top_points_right", 0) >= SIDEWAYS:
        return ROTATION["top_points_left" if p["top_points_left"] > p["top_points_right"] else "top_points_right"]
    return 0


def cut_line(img: PILImage.Image, box) -> PILImage.Image:
    """The stamp's Lat/Long line cut from the full-resolution image, by a Box answer's edges (fractions of
    the width and height), with a little room around it so no digit is cut off.
    """
    w, h = img.size
    tall = (box.bottom - box.top) * h
    edges = (box.left * w - 0.03 * w, box.top * h - 0.6 * tall, box.right * w + 0.03 * w, box.bottom * h + 0.6 * tall)
    left, top, right, bottom = (round(min(max(e, 0), side)) for e, side in zip(edges, (w, h, w, h)))
    return img.crop((left, top, max(right, left + 1), max(bottom, top + 1)))


def read_line(client: TypeSafeClient, img: PILImage.Image, raw: dict, model: str) -> dict:
    """Add the position picked to four decimals (`lat_pick`, `long_pick`) to an unloading photo's answers.

    The stamp's Lat/Long line is found with a Box question (on BOX_MODEL), cut out of the upright
    full-resolution photo, and Jev picks each value from the 100 that start with its two-decimal reading
    (`starts_of`). Nothing is added when that reading has an unread part.
    """
    a = raw["answers"]
    lat, long = starts_of(a, "lat"), starts_of(a, "long")
    if not lat or not long:
        return raw
    found = client.system_one(state={"photo": Image.from_pil(fit(img), format="jpeg")}, questions={"line": LINE},
                              model=BOX_MODEL)
    box = found.answers["line"]
    line = Image.from_pil(fit(cut_line(img, box.box)), format="jpeg")
    questions = {"lat_pick": pick("latitude", "Lat or Latitude", lat), "long_pick": pick("longitude", "Long, Lng or Longitude", long)}
    picked = client.system_one(state={"line": line}, questions=questions, model=model)
    tokens = (found.usage.input_tokens or 0) + (picked.usage.input_tokens or 0)
    return {**raw, "requests": raw["requests"] + 2, "input_tokens": raw["input_tokens"] + tokens,
            "line": {**box.box.model_dump(), "confidence": box.confidence},
            "answers": {**a, **{q: summary(x) for q, x in picked.answers.items()}}}


def analyze(client: TypeSafeClient | None, slot: str, img: PILImage.Image, vehicle_number: str, model: str,
            quantity: str | None = None, cache: dict | None = None, key: str | None = None,
            place: bool = False, seal: bool = False) -> tuple[dict, PILImage.Image]:
    """Check one upright-by-EXIF RGB image in its slot. Returns the result and the image as finally sent.

    `quantity` is the typed quantity in kilograms, which a bill's net weight must match. With `place`,
    a photo's GPS stamp position is read too, to four decimals where it can be (`read_line`). With
    `seal`, a bill is asked about the weighbridge's stamp. With `cache` and `key`, saved answers are
    used instead of asking Jev, and new answers are saved.
    """
    if cache is not None and key in cache:
        raw = cache[key]
        result = {**result_from(slot, raw, vehicle_number, quantity), "saved": True}
        return result, fit(img.rotate(raw["rotated"], expand=True))
    response = ask(client, slot, img, vehicle_number, model, quantity, place, seal)
    tokens, requests, rotated = response.usage.input_tokens or 0, 1, 0
    turned = response.choices["orientation"]
    looks = [summary(turned)]  # each orientation answer, before each turn
    # At most two turns: the likelier sideways guess, then half a turn if that left it upside down.
    while requests < 3 and (turn := turn_for(response.choices["orientation"], first=requests == 1)):
        img = img.rotate(turn, expand=True)
        rotated = (rotated + turn) % 360
        response = ask(client, slot, img, vehicle_number, model, quantity, place, seal)
        tokens += response.usage.input_tokens or 0
        requests += 1
        looks.append(summary(response.choices["orientation"]))
    raw = {"model": response.model, "requests": requests, "orientation": summary(turned), "rotated": rotated, "looks": looks,
           "answers": {q: summary(a) for q, a in response.answers.items()}, "input_tokens": tokens}
    if place:
        raw = read_line(client, img, raw, model)
    if cache is not None and key:
        cache[key] = raw
    return result_from(slot, raw, vehicle_number, quantity), fit(img)


def analyze_trip(client: TypeSafeClient | None, uploads: dict[str, list[Upload]], vehicle_number: str, model: str,
                 quantity: str | None = None, limits: Limits = Limits(), index: ImageIndex | None = None,
                 trip: str | None = None, cache: dict | None = None,
                 plant: tuple[float, float] | None = None, verra: bool = False) -> tuple[dict, dict]:
    """Check every image of a trip in parallel and decide.

    Returns ({"images": {role: [result]}, "verdict": ...}, {role: [image as sent]}). An image uploaded
    twice in the trip is asked about once. With `index`, images already used in other trips (not
    `trip` itself) are flagged. `cache` holds saved answers (see `analyze`). With `plant`, the
    plant's (latitude, longitude), unloading photos are read for where they were taken and checked
    against it. With `verra` (a Verra project trip), bills are checked for the weighbridge's stamp.
    """
    jobs = []  # (role, upload, image, fingerprints)
    for role in ROLES:
        for u in uploads.get(role, []):
            img = load_bytes(u.data)
            jobs.append((role, u, img, fingerprints(u.data, img)))
    first_of = {}
    for _, u, _, prints in jobs:
        first_of.setdefault(prints["md5"], u.name)
    # Asked where it was taken: an unloading photo, also when the same file is a loading photo too.
    placed = {prints["md5"] for role, _, _, prints in jobs if role == "unloading"} if plant else set()

    def run(job):
        role, u, img, prints = job
        slot, place = SLOT_OF[role], prints["md5"] in placed
        seal = verra and slot == "bill"
        return analyze(client, slot, img, vehicle_number, model, quantity, cache,
                       answer_key(prints, slot, vehicle_number, quantity, model, place, seal), place, seal)

    unique = [j for j in jobs if first_of[j[3]["md5"]] == j[1].name]
    with ThreadPoolExecutor(max(1, min(8, len(unique)))) as pool:
        done = dict(zip((j[3]["md5"] for j in unique), pool.map(run, unique)))
    images, sent = {role: [] for role in ROLES}, {role: [] for role in ROLES}
    for role, u, _, prints in jobs:
        result, img = done[prints["md5"]]
        repeat = first_of[prints["md5"]] != u.name
        images[role].append({**result, "name": u.name, "prints": prints, "requests": 0 if repeat else result["requests"],
                             "input_tokens": 0 if repeat else result["input_tokens"],
                             "cost_usd": 0 if repeat else result["cost_usd"], "same_as": first_of[prints["md5"]] if repeat else None})
        sent[role].append(img)
    extra = [reuse_check(images, index, trip)] if index is not None else []
    return {"images": images, "verdict": verdict(images, vehicle_number, quantity, limits, extra, plant, verra)}, sent


MARK = {"pass": "ok  ", "fail": "FLAG", "review": "??  "}


def print_trip(trip: dict, vehicle_number: str, limits: Limits, plant: tuple[float, float] | None = None,
               verra: bool = False) -> None:
    for role in ROLES:
        for r in trip["images"][role]:
            rotated = f", rotated {r['rotated']}°" if r["rotated"] else ""
            same = f", same file as {r['same_as']}" if r["same_as"] else ""
            print(f"\n{ROLE_TITLES[role]}: {r['name']}  (answered by {r['model']}{rotated}{same})")
            for c in r["checks"]:
                print(f"  {MARK[c['status']]} {c['label']:<26} {c['detail']}  ({c['certainty']:.2f})")
            for reading in r["readings"]:
                if not reading["missing"]:
                    print(f"       {reading['label']:<26} {reading['value']}  ({reading['min_confidence']:.2f})")
    v = trip["verdict"]
    tokens = sum(r["input_tokens"] for rs in trip["images"].values() for r in rs)
    print(f"\nVehicle {vehicle_number}: {v['title']}")
    near = f", unloading stamped within {distance(limits.plant_radius)} of the plant at {plant[0]}, {plant[1]}" if plant else ""
    print(f"  Limits: tare first, tare to gross at least {duration(limits.min_gap_tare_first)}; gross first, gross to tare "
          f"at least {duration(limits.min_gap_gross_first)}; clock slack {duration(limits.clock_slack)}{near}")
    if verra:
        print("  Verra project: the final slip must carry the weighbridge's stamp")
    print("  Timeline: " + " -> ".join(f"{e['what']} {e['time']}{' (!)' if e['bad'] else ''}" for e in v["timeline"]))
    for c in v["flags"] + [c for c in v["checks"] if c["status"] == "pass" and (c["group"] == "times" or c["key"] in ("unloading_plant", *SEAL))]:
        print(f"  {MARK[c['status']]} {GROUPS[c['group']]} · {c['label']}: {c['detail']}")
    print(f"  {tokens} input tokens ≈ ${tokens * PRICE_PER_INPUT_TOKEN:.5f}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("vehicle_number", help="the trip's vehicle number, such as KL05AF9341")
    parser.add_argument("--trip", type=Path, help="a trip folder: collection*, unloading* and weighbill* images")
    parser.add_argument("--loading", type=Path, nargs="+", default=[], help="loading photos, with GPS stamps")
    parser.add_argument("--unloading", type=Path, nargs="+", default=[], help="unloading photos, with GPS stamps")
    parser.add_argument("--bill", type=Path, nargs="+", default=[], help="weighbridge slips")
    parser.add_argument("--quantity", help="the quantity in kilograms, such as 2570, which the final slip's net weight must match")
    parser.add_argument("--plant", help='the plant\'s latitude, longitude, such as "11.4404, 75.9420": unloading photos '
                                        "must be stamped near it. Without it, where they were taken isn't checked.")
    parser.add_argument("--verra", action="store_true", help="a Verra project trip: the final slip must carry the weighbridge's stamp")
    for f in fields(Limits):
        parser.add_argument(f"--{f.name.replace('_', '-')}", dest=f.name, metavar={"minutes": "MIN", "metres": "M"}[LIMIT_UNITS[f.name]],
                            help=f"{LIMIT_NAMES[f.name]}, in {LIMIT_UNITS[f.name]} (default {f.default})")
    parser.add_argument("--others", type=Path, help="a folder of other trips, to flag images used again (such as trips/)")
    parser.add_argument("--model", default="jev-latest", help="jev-latest (1.13), jev-preview (1.14), or a pinned ID")
    args = parser.parse_args()

    number = normalize_vehicle_number(args.vehicle_number)
    if not number:
        parser.error(f"{args.vehicle_number!r} doesn't look like a vehicle number. Use 4 to 12 letters and digits.")
    try:
        quantity = parse_quantity(args.quantity)
        plant = parse_plant(args.plant)
        limits = parse_limits(vars(args))
    except ValueError as error:
        parser.error(str(error))
    uploads = read_trip_folder(args.trip) if args.trip else {role: [] for role in ROLES}
    for role in ROLES:
        uploads[role] += [Upload(p.name, p.read_bytes()) for p in getattr(args, role)]
    if not any(uploads.values()):
        parser.error("Give --trip, or images with --loading, --unloading and --bill.")
    index = ImageIndex.of_folders(args.others) if args.others else None
    trip_id = args.trip.resolve().relative_to(args.others.resolve()).as_posix() \
        if args.trip and args.others and args.trip.resolve().is_relative_to(args.others.resolve()) else None

    if not (key := api_key()):
        sys.exit(NO_KEY)
    with TypeSafeClient(api_key=key, headers=HEADERS, timeout=TIMEOUT) as client:
        try:
            trip, _ = analyze_trip(client, uploads, number, args.model, quantity, limits, index, trip_id, plant=plant,
                                   verra=args.verra)
        except Exception as error:
            sys.exit(f"{type(error).__name__}: {error}")
    print_trip(trip, number, limits, plant, args.verra)


if __name__ == "__main__":
    main()
