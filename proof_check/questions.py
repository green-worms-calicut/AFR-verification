"""The questions Jev is asked about each image (`QUESTIONS`, per slot: `photo` for loading and unloading
photos, `bill`), and the Box questions that find a bill's slip (`SLIP`) and a GPS stamp's position line (`LINE`).

Questions only ask; every rule and decision on the answers is in trip_rules. `question_version` changes whenever
a question does, so answers saved for older questions aren't reused.
"""

import hashlib
import json

from typesafe_sdk import Box, Choice, Noul, NoulCriteria

NOT_SHOWN = "not_shown"

SLOTS = ("photo", "bill")  # the two kinds of image Jev is asked about


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
PRINTED = ("If `bill` prints one date and time for the whole slip, at its top or in its heading rather than beside a "
           "weight (the date may be labelled Bill Date or Date),")
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
        # On 19 Oct 2026 the minute was the unsure part of 19 of 30 unsure bill times, often with the seconds
        # second (07:02:14 read 02 at 0.76, 14 at 0.13), and once read as the seconds (09:46:54 as 09:54).
        f"{prefix}_minute": Choice(instructions=f"{premise} what minute is that time: the two digits right after the "
                                                f"hour? If seconds are printed too, as in 07:02:14, the minute is the "
                                                f"middle pair (02), not the last (14). {NOT_CAMERA}",
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
        # A slip shown on the weighbridge's computer could be typed or changed and never printed (27 Oct 2026: 172913).
        "screen_photo": Noul(
            instructions="Is `bill` a picture of a computer or phone screen showing the slip, rather than a photo of a "
                         "printed paper slip?",
            criteria=NoulCriteria(
                true="A screen was photographed: screen pixels or wavy moiré lines, a screen's edge or frame, glare "
                     "on glass, or a program's window bars or buttons show.",
                false="A photo taken directly of a printed paper slip.",
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
        # Lakshmi slips print no time beside the weights, only this one, so a trip's weighing times come from its
        # first and final slips' (`weighings`).
        **bill_time("printed", PRINTED),
        # Compares the weights rather than reading labels: Kuttiyadi slips print First Wt and Second Wt with no
        # Time In, and label both times Gross (22 Oct 2026).
        "first_weight": Choice(
            instructions="On `bill`, is the weight taken first (at the Time In, or labelled First Wt or First Weight) "
                         "lighter or heavier than the weight taken second (at the Time Out, or labelled Second Wt or "
                         "Second Weight)? Compare the two numbers, whatever else they are labelled.",
            criteria={
                "tare": "The first weight is the smaller number: the vehicle was weighed empty first.",
                "gross": "The first weight is the larger number: the vehicle was weighed loaded first.",
                NOT_SHOWN: "The bill doesn't show which weight was taken first, or the two weights can't be read.",
            },
        ),
        # Whether anything is entered, not what: Jev can't read the material out. `filled` isn't first (see `bill_number`).
        # Only asked on a Verra project trip (`VERRA`).
        # Lakshmi slips have no label but print "Loaded with kyilandi plastic" (23 Oct 2026: Jev 0.66-0.86 sure
        # it was filled before "Loaded with" was named). EMPTY or LOAD alone names no material.
        "material": Choice(
            instructions="Is the material or product carried filled in on `bill`? The bill may label it Material, "
                         "Product, Item, Commodity or Goods, or write it after Loaded with.",
            criteria={
                "blank": "The bill has such a label, but nothing is entered next to it, or only Nil, NA, a dash or 0.",
                "empty_or_load": "The entry only says the vehicle is empty or loaded, such as EMPTY, LOAD or LOADED, "
                                 "without naming any material.",
                "no_field": "The bill has no Material, Product or similar label, and no Loaded with line.",
                "filled": "A material is named, such as Plastic, Mixed Waste or Cloth, printed or handwritten, "
                          "including after Loaded with.",
            },
        ),
        # Whether the weighbridge's stamp is there, and whose name it shows, are asked apart: on 22 Oct 2026 one
        # question for both was unsure (0.45 to 0.79) on 2 of 3 clearly stamped slips. Only asked on a Verra project trip.
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
# The questions about the weighbridge's stamp (seal) on a bill, and our company's name as on its own stamp, sent
# with them so that stamp isn't taken for the weighbridge's.
SEAL = ("weighbridge_seal", "weighbridge_name")
# The bill questions asked only on a Verra project trip: the material, and the stamp.
VERRA = ("material", *SEAL)
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
        ("printed_time", "print time", ["printed_hour", "printed_minute", "printed_ampm"], ":"),
        ("printed_date", "print date", ["printed_day", "printed_month"], "/"),
    ],
}


def questions_for(slot: str, quantity: str | None, place: bool = False, verra: bool = False) -> dict:
    """The questions asked about one image: the net weight only when a quantity was typed, where
    the photo was taken only with `place` (an unloading photo, when the plant's location is given),
    and the material and the weighbridge's stamp only with `verra` (a bill of a Verra project trip).
    """
    return {q: v for q, v in QUESTIONS[slot].items()
            if (quantity or q != "net_weight") and (place or q not in PLACE) and (verra or q not in VERRA)}


def question_version(slot: str, place: bool = False, verra: bool = False) -> str:
    """Changes whenever a question changes, so saved answers from older questions aren't reused.

    The place and Verra questions only count with `place` and `verra`, so adding them kept the answers
    saved before. With the Verra questions, our company's name counts too, as it is sent with them.
    """
    asked = {q: v for q, v in QUESTIONS[slot].items() if (place or q not in PLACE) and (verra or q not in VERRA)}
    if place:  # and how the line is found and picked from
        asked |= {"line": LINE, "pick": pick("{axis}", "{labels}", ["{start}", "{other}"])}
    if slot == "bill":  # and how the slip is found and cut out
        asked |= {"slip": SLIP}
    text = json.dumps({q: v.model_dump(mode="json") for q, v in asked.items()}, sort_keys=True) \
        + (BOX_MODEL if place or slot == "bill" else "") \
        + (OUR_COMPANY if verra else "")
    return hashlib.md5(text.encode()).hexdigest()[:10]


# Every bill is cut to its slip before it is read (`analyze`). Bills photographed as a strip across a tall
# phone photo end up in small print once scaled to MAX_SIDE. On 21 final slips of 19 Oct 2026 (23 Oct run),
# times read on the cut-out slip were right 21 times and sure 15, against 20 and 13 on the whole photo;
# a Maliyekkal slip 19% of its photo went from wrong to right and 0.97 sure.
SLIP = Box(instructions="Find the printed weighbridge slip (weighment slip) in `bill`: the whole paper slip, from its "
                        "heading to its last printed line. If the same slip is printed twice, as an original and a "
                        "duplicate copy, find the upper copy only.")
