"""The rules a trip is checked by, from Jev's answers: each image's checks and readings, the weighings and
their order and gap, every photo's timing, unloading at the plant, the final slip, the Verra checks, and images
or trips used twice. Every check is in one of three parts (`PARTS`, `PART_OF`); `Limits` are the timing and
distance rules, changeable per run.

The confidence thresholds here are placeholders until run_trips and evaluate reports on real images set them.
"""

import itertools
import math
from dataclasses import dataclass
from datetime import datetime

from proof_check.questions import HOURS, NOT_SHOWN, PICKS, PLACE, QUESTIONS, REPORT, SEAL
from proof_check.images import ImageIndex

# Placeholder thresholds: choose real ones from evaluate.py reports on labeled images.
CONFIDENT = 0.8  # a Choice answer below this confidence goes to a person
YES, NO = 0.8, 0.2  # a yes/no answer between these goes to a person
# Thresholds measured for one question: (yes at or above, no at or below). In the 19 Oct 2026 run of 42 real
# trips, every photo taken directly scored 0.05-0.43 for screen_photo, and every photo of a screen 0.99.
NOUL_THRESHOLDS = {"screen_photo": (0.8, 0.6)}


@dataclass(frozen=True)
class Limits:
    """The timing rules, in minutes, and how near the plant unloading must be, in metres. The defaults can be changed for each run."""

    min_gap_tare_first: int = 60  # loading happens between the weighings, so they are at least this far apart
    min_gap_gross_first: int = 10  # unloading happens between the weighings, so they are at least this far apart
    clock_slack: int = 5  # the camera's clock and the weighbridge's may disagree by this much
    # Weighings on different days but no further apart than this are a trip across midnight; further, a stored
    # tare (27 Oct 2026: 21 to 25 h on 172907, 172940, 173097).
    max_overnight_gap: int = 720
    # How far from the plant an unloading photo's square (`plant_check`) may be. Plant photos on 19 Oct 2026
    # were stamped within 51 m of it; the nearest unloading elsewhere was 2.9 km away.
    plant_radius: int = 500


# How each limit is described in error messages and the command-line help, and its unit.
LIMIT_NAMES = {
    "min_gap_tare_first": "the least gap between the tare and gross weighings, when tare is first",
    "min_gap_gross_first": "the least gap between the gross and tare weighings, when gross is first",
    "clock_slack": "how far a photo's clock may be off the weighbridge's",
    "max_overnight_gap": "the most gap between weighings on different days (a trip across midnight)",
    "plant_radius": "how far from the plant an unloading photo's stamp may place it",
}
LIMIT_UNITS = {"min_gap_tare_first": "minutes", "min_gap_gross_first": "minutes", "clock_slack": "minutes", "max_overnight_gap": "minutes",
               "plant_radius": "metres"}

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
    "material": ("Material entered", {
        "filled": ("pass", "A material is entered"),
        "blank": ("fail", "No material entered"),
        "empty_or_load": ("fail", "Material entered as EMPTY or LOAD, not a material"),
        "no_field": ("review", "No material field on the slip"),
    }),
    "weighbridge_name": ("Weighbridge stamp", {
        "same": ("pass", "The weighbridge's own stamp is on it, with the heading's name"),
        "different": ("fail", "Stamped by a different weighbridge from the one in the heading"),
        NOT_SHOWN: ("review", "The stamp's name can't be compared with the heading"),
    }),
}
# Bill questions that only count on the final slip: its net weight, labels and material, and the stamp proving it.
FINAL_ONLY = ("net_weight", "gross_vs_tare", "material", *SEAL)
GROUPS = {"loading": "Loading photos", "unloading": "Unloading photos", "bill": "Weigh bills",
          "times": "Times", "trips": "Other trips"}
# The three parts of a trip's check, each decided, and accepted or rejected, on its own: did the trip happen as
# claimed, is its proof complete, and what a Verra project trip also needs.
PARTS = {"real": "Trip is real", "docs": "Documentation", "verra": "Verra"}
# Which part each of a trip's checks (by key) is in. A missing image can't be checked for being real, so its
# plate or times ask a person in the real part too.
PART_OF = {
    "loading_plate": "real", "unloading_plate": "real", "bill_number": "real",
    "weigh_order": "real", "weigh_gap": "real", "weigh_day": "real", "time_in_tare": "real",
    "loading_timing": "real", "unloading_timing": "real", "unloading_day": "real",
    "loading_direct": "real", "unloading_direct": "real", "bill_direct": "real", "unloading_plant": "real",
    "reused": "real", "overlap": "real",
    "loading_missing": "docs", "unloading_missing": "docs", "bill_missing": "docs",
    "loading_vehicle": "docs", "unloading_vehicle": "docs", "loading_stamp": "docs", "unloading_stamp": "docs",
    "bill_found": "docs", "net_weight": "docs", "gross_vs_tare": "docs",
    "material": "verra", "weighbridge_seal": "verra",
    # The verifier: an image gw_sales listed that isn't in S3 or isn't an image; one file as both a collection and an
    # unloading photo; a photo stamped within minutes of one in another disposal of the same vehicle.
    "image_unavailable": "docs", "same_file": "real", "same_moment": "real",
    # The verifier, from what was entered in gw_sales: every collection log has a photo, every collection photo was
    # taken on its pickup day, no unloading photo long after the end time, and the collections before the arrival.
    "collection_has_photos": "docs", "collection_photo_date": "real", "unloading_too_late": "real",
    "disposal_after_collections": "docs", "weighbridge_matches": "docs", "collection_point": "real",
}


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
            parts[0] = same_hour(parts[0], parts[2]["choice"])
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


def same_hour(hour: dict, ampm: str) -> dict:
    """The hour answer, as sure as all its options that make the same 24-hour time with `ampm`.

    Asked for the hour as printed, Jev is often torn between that and the 24-hour hour: 02:45:57 PM read 02 at
    0.54 and 14 at 0.46 (23 Oct 2026, trip 173284). With PM both are 14:00, so the hour is surely 14.
    """
    target = to_24h(f"{hour['choice']}:00", ampm)
    alike = sum(p for option, p in hour.get("top", []) if option != hour["choice"] and option in HOURS
                and to_24h(f"{option}:00", ampm) == target)
    return {**hour, "confidence": min(1.0, hour["confidence"] + alike)} if alike else hour


def lower_first(text: str) -> str:
    """'No GPS stamp' -> 'no GPS stamp', leaving a word in capitals such as 'GPS' as it is."""
    return text if text[1:2].isupper() else text[:1].lower() + text[1:]


def unsure(detail: str, certainty: float) -> str:
    """A check's detail when Jev isn't sure enough of its answer: 'Only 62% sure: plate shows a different number'."""
    return f"Only {certainty:.0%} sure: {lower_first(detail)}"


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
            detail = {"pass": yes, "fail": no, "review": f"Not sure: {lower_first(yes if pass_when_yes else no)}?"}[status]
            checks.append({"key": qid, "label": label, "status": status, "detail": detail,
                           "certainty": a["yes"], "measure": "yes"})
        elif qid in CHOICE_CHECKS:
            label, outcomes = CHOICE_CHECKS[qid]
            status, detail = outcomes[a["choice"]]
            if a["confidence"] < CONFIDENT:
                status, detail = "review", unsure(detail, a["confidence"])
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


def weighings(bill: dict, first_slip: dict | None = None) -> dict | None:
    """When the gross and tare weights were taken: {"gross": moment, "tare": moment, "certainty", "source", "slips"},
    `slips` naming the bills the times were read from.

    A bill prints a date and time with each weight, or only a Time In and Time Out (or a first and second
    weighing time), with `first_weight` saying whether the first weight is the tare (lighter) or the gross
    (heavier). Or, as Lakshmi's, only one time at the top of each slip: then the first weighing is when
    `first_slip` (one weight) was printed, and the second when this final slip was. The surest layout that
    can be read is used. None if none can be read.
    """
    r = {x["key"]: x for x in bill["readings"]}
    found = []
    gross, tare = moment(r["gross_time"], r["gross_date"]), moment(r["tare_time"], r["tare_date"])
    if gross and tare:
        found.append({"gross": gross, "tare": tare, "certainty": min(gross["certainty"], tare["certainty"]),
                      "source": "the times printed with each weight", "slips": [bill["name"]]})
    first = bill["answers"]["first_weight"]
    time_in, time_out = moment(r["in_time"], r["in_date"]), moment(r["out_time"], r["out_date"])
    if time_in and time_out and first["choice"] != NOT_SHOWN:
        gross, tare = (time_in, time_out) if first["choice"] == "gross" else (time_out, time_in)
        found.append({"gross": gross, "tare": tare,
                      "certainty": min(time_in["certainty"], time_out["certainty"], first["confidence"]),
                      "source": f"Time In and Time Out, {first['choice']} first", "slips": [bill["name"]]})
    if first_slip and first["choice"] != NOT_SHOWN:
        before = {x["key"]: x for x in first_slip["readings"]}
        earlier, later = moment(before["printed_time"], before["printed_date"]), moment(r["printed_time"], r["printed_date"])
        if earlier and later:
            gross, tare = (earlier, later) if first["choice"] == "gross" else (later, earlier)
            found.append({"gross": gross, "tare": tare,
                          "certainty": min(earlier["certainty"], later["certainty"], first["confidence"]),
                          "source": f"the times printed at the top of the first and final slips, {first['choice']} first",
                          "slips": [first_slip["name"], bill["name"]]})
    return max(found, key=lambda w: w["certainty"], default=None)


def first_slip(bills: list[dict]) -> dict | None:
    """The slip from the first weighing, with one weight: the surest one."""
    slips = [b for b in bills if b["answers"]["weigh_bill"]["yes"] >= 0.5 and b["answers"]["slip_kind"]["choice"] == "one_weight"]
    return max(slips, key=lambda b: b["answers"]["slip_kind"]["confidence"], default=None)


def check(group: str, key: str, label: str, status: str, detail: str, certainty: float | None = None,
          images: list[str] = (), cause: str | None = None) -> dict:
    """One of a trip's checks. `group` is the kind of image or rule it is about, `part` the part it counts in.

    `detail` is the reason in a sentence a person can act on. `images` are the names of the images to look at,
    and `cause` is shared by checks flagged for the same reason, such as one unsure reading of the weighing
    times, so the page can show them as one.
    """
    return {"group": group, "part": PART_OF[key], "key": key, "label": label, "status": status, "detail": detail,
            "certainty": certainty, "measure": "confidence" if certainty is not None else None,
            "images": list(images), "cause": cause}


def names(results: list[dict]) -> str:
    return and_list([r["name"] for r in results])


def named(results: list[dict]) -> list[str]:
    return [r["name"] for r in results]


def plural(results: list, one: str, many: str) -> str:
    return one if len(results) == 1 else many


# What a failed or unsure photo check says, as (problem, unsure) sentences about {names}.
EACH_PHOTO = {
    "shows_vehicle": ("No vehicle in {names}.", "Not sure there's a vehicle in {names}. Look for it."),
    "gps_stamp": ("No GPS stamp on {names}.", "Not sure there's a GPS stamp on {names}. Look for it."),
    "screen_photo": ("Photographed off a screen, not taken directly: {names}.",
                     "Not sure {names} {was} taken directly, not photographed off a screen."),
}


def each_photo(role: str, key: str, label: str, results: list[dict], qid: str) -> dict:
    """A yes/no question that every photo of a role must pass, such as "shows a vehicle"."""
    pass_when_yes = NOUL_CHECKS[qid][3]
    problem, doubt = EACH_PHOTO[qid]
    statuses = {r["name"]: noul_status(r["answers"][qid]["yes"], pass_when_yes, qid) for r in results}
    failed = [r for r in results if statuses[r["name"]] == "fail"]
    unsure = [r for r in results if statuses[r["name"]] == "review"]
    if failed:
        return check(role, key, label, "fail", problem.format(names=names(failed)), images=named(failed))
    if unsure:
        return check(role, key, label, "review", doubt.format(names=names(unsure), was=plural(unsure, "was", "were")),
                     images=named(unsure))
    return check(role, key, label, "pass", f"{len(results)} of {len(results)} photos")


def plate_check(role: str, results: list[dict], vehicle_number: str) -> dict:
    """At least one photo of the role clearly shows the typed number; any photo clearly showing another is a problem."""
    label = f"{role.capitalize()} plate matches {vehicle_number}"
    sure = [r for r in results if r["answers"]["plate_number"]["confidence"] >= CONFIDENT]
    different = [r for r in sure if r["answers"]["plate_number"]["choice"] == "different"]
    same = [r for r in sure if r["answers"]["plate_number"]["choice"] == "same"]
    if different:
        return check(role, f"{role}_plate", label, "fail",
                     f"{names(different)} {plural(different, 'shows', 'show')} a different number plate from {vehicle_number}.",
                     images=named(different))
    if same:
        return check(role, f"{role}_plate", label, "pass", f"Seen clearly in {len(same)} of {len(results)}",
                     max(r["answers"]["plate_number"]["confidence"] for r in same))
    return check(role, f"{role}_plate", label, "review",
                 f"No {role} photo shows the number plate clearly enough to compare with {vehicle_number}. "
                 "Look for the plate in the photos.", images=named(results))


def photo_checks(role: str, results: list[dict], vehicle_number: str) -> list[dict]:
    if not results:
        cause = f"{role}_missing"
        return [check(role, f"{role}_missing", GROUPS[role], "review", f"No {role} photos added.", cause=cause),
                check(role, f"{role}_plate", f"{role.capitalize()} plate matches {vehicle_number}", "review",
                      f"No {role} photos to compare with {vehicle_number}.", cause=cause)]
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
    """At least one unloading photo stamped within `plant_radius` of the plant; any stamped farther is a problem
    (`near_check`)."""
    return near_check("unloading", "unloading_plant", "At the plant", results, plant, limits.plant_radius,
                      "the plant", "unloading")


def near_check(group: str, key: str, label: str, results: list[dict], point: tuple[float, float], radius: float,
               point_name: str, doing: str) -> dict:
    """At least one of the photos in `results` stamped within `radius` of `point`; any stamped farther is a problem.
    `point_name` names the point in the details ("the plant") and `doing` what happens there ("unloading").

    Within the radius takes a position picked surely to four decimals (a square about 11 m across), so the
    distance is known to the metre. Away can also be shown by the two-decimal reading: when even the
    nearest point of its 1.1 km square (`from_square`) is past the radius, read surely or by `chance_away`.
    But a two-decimal "away" against another photo surely near asks a person: on 19 Oct 2026 two photos
    at the plant were read 11.40 or 11.14 for 11.44, 84% sure. Like the plate, one photo is enough to
    pass, and anything less sure asks a person.
    """
    near, away, unsure, unread = [], [], [], []  # near and away: (result, detail, certainty, metres, to four decimals)
    for r in results:
        p = next((x for x in r["readings"] if x["key"] == "position"), None)
        if not p:
            unread.append(r)
            continue
        if not p["missing"] and p["value"] != p["coarse"]["value"] and p["min_confidence"] >= CONFIDENT:
            m = from_square(p["value"], point)
            (near if m <= radius else away).append((r, f"at {p['value']}, {distance(m)} from {point_name}", p["min_confidence"], m, True))
            continue
        c = p["coarse"]
        if not c["missing"] and c["min_confidence"] >= CONFIDENT:
            if (m := from_square(c["value"], point)) > radius:
                away.append((r, f"at {c['value']}, at least {distance(m)} from {point_name}", c["min_confidence"], m, False))
                continue
        elif (no := chance_away(r["answers"], point, radius)) >= YES:
            read = f"read as {c['value']}" if c["value"] else "partly read"
            away.append((r, f"away from {point_name} ({no:.0%} likely, {read})", no, None, False))
            continue
        (unread if c["missing"] else unsure).append(r)
    closest = min(near, key=lambda x: x[3], default=None)
    if away and (any(x[4] for x in away) or not near):
        r, detail, c, _, _ = next((x for x in away if x[4]), away[0])
        return check(group, key, label, "fail",
                     f"{r['name']} is stamped {detail}, but {doing} must be within {distance(radius)} of it.", c,
                     [r["name"]])
    if away:
        (r, detail, _, _, _), (n, there, c, _, _) = away[0], closest
        return check(group, key, label, "review",
                     f"The photos disagree: {r['name']} seems stamped {detail}, but {n['name']} is {there}. "
                     "Check both GPS stamps.", c, [r["name"], n["name"]])
    if near:
        r, detail, c, _, _ = closest
        return check(group, key, label, "pass",
                     f"{len(near)} of {len(results)} stamped within {distance(radius)}: {r['name']} {detail}", c)
    if unsure:
        return check(group, key, label, "review",
                     f"The position on the GPS stamp of {names(unsure)} couldn't be read surely enough to tell if "
                     f"{doing} was within {distance(radius)} of {point_name}. Check the Lat/Long line.", images=named(unsure))
    return check(group, key, label, "review",
                 f"No position could be read on the GPS stamp of {names(unread)}. Check the Lat/Long line places "
                 f"{doing} within {distance(radius)} of {point_name}.", images=named(unread))


def final_slip(bills: list[dict]) -> dict | None:
    """The slip with both weights and the net, the one the times and net weight come from."""
    slips = [b for b in bills if b["answers"]["weigh_bill"]["yes"] >= 0.5]
    finals = [b for b in slips if b["answers"]["slip_kind"]["choice"] == "both_weights"]
    return max(finals, key=lambda b: b["answers"]["slip_kind"]["confidence"], default=None)


# What to look at when Jev isn't sure of a final-slip answer.
LOOK_FOR = {
    "net_weight": "Check the net weight on the slip is {quantity} kg.",
    "gross_vs_tare": "Check which weight is labelled gross.",
    "material": "Check the slip's material field.",
    "weighbridge_name": "Compare the stamp's name with the slip's heading.",
}


def choice_check(group: str, key: str, qid: str, answer: dict, vehicle_number: str, quantity: str | None,
                 bill: dict | None = None) -> dict:
    """A check from one Choice answer about `bill`, named in the detail, with what to look at when unsure."""
    label, outcomes = CHOICE_CHECKS[qid]
    status, detail = outcomes[answer["choice"]]
    if answer["confidence"] < CONFIDENT:
        status, detail = "review", unsure(detail, answer["confidence"]) + ". " + LOOK_FOR.get(qid, "")
    typed = {"number": vehicle_number, "quantity": quantity}
    prefix = f"{bill['name']}: " if bill else ""
    return check(group, key, label.format(**typed), status, prefix + detail.format(**typed).strip(), answer["confidence"],
                 [bill["name"]] if bill and status != "pass" else [])


def seal_checks(final: dict | None) -> list[dict]:
    """The final slip carries the weighbridge's own stamp.

    It passes when it is surely there and its name surely matches the slip's heading; a name surely another
    weighbridge's is a problem, and an unsure one goes to a person.
    """
    if not final:
        return [check("bill", "weighbridge_seal", "Weighbridge stamp", "review",
                      "No final slip to look for the weighbridge's stamp on.", cause="no_final")]
    a = final["answers"]
    theirs, name = a["weighbridge_seal"]["yes"], a["weighbridge_name"]
    status = noul_status(theirs, qid="weighbridge_seal")
    if status == "pass":
        return [choice_check("bill", "weighbridge_seal", "weighbridge_name", name, "", None, final)]
    detail = {"fail": "no weighbridge stamp found. The final slip must carry the weighbridge's own stamp.",
              "review": f"only {theirs:.0%} likely the weighbridge's own stamp is on it. Look for the stamp."}[status]
    return [check("bill", "weighbridge_seal", "Weighbridge stamp", status, f"{final['name']}: {detail}", theirs,
                  [final["name"]])]


def verra_checks(final: dict | None) -> list[dict]:
    """What a Verra project trip also needs of the final slip: a material entered, and the weighbridge's own stamp."""
    if final:
        material = choice_check("bill", "material", "material", final["answers"]["material"], "", None, final)
    else:
        material = check("bill", "material", "Material entered", "review", "No final slip to look for the material on.",
                         cause="no_final")
    return [material, *seal_checks(final)]


def bill_checks(bills: list[dict], vehicle_number: str, quantity: str | None, verra: bool = False,
                weighbridge: str | None = None) -> list[dict]:
    """A weighbridge slip, the final slip, the vehicle number on every slip, the final slip's net weight and labels,
    for a Verra project trip, its material and stamp, and with `weighbridge`, that it is from that weighbridge.
    """
    if not bills:
        return [check("bill", "bill_missing", "Weigh bills", "review", "No weigh bills added.", cause="bill_missing"),
                check("bill", "bill_number", f"Bills match {vehicle_number}", "review",
                      f"No weigh bills to compare with {vehicle_number}.", cause="bill_missing"),
                *(verra_checks(None) if verra else []),
                *([weighbridge_check(None, weighbridge, cause="bill_missing")] if weighbridge else [])]
    slips = [b for b in bills if b["answers"]["weigh_bill"]["yes"] >= 0.5]
    ignored = [b for b in bills if b not in slips]
    note = f" (ignored as not a slip: {names(ignored)})" if ignored else ""
    final = final_slip(bills)
    checks = []
    if not any(b["answers"]["weigh_bill"]["yes"] >= YES for b in slips):
        checks.append(check("bill", "bill_found", "Weighbridge slip", "review",
                            f"No image is clearly a weighbridge slip{note}. Check the bills.", images=named(bills),
                            cause=None if final else "no_final"))
    elif final and final["answers"]["slip_kind"]["confidence"] >= CONFIDENT:
        checks.append(check("bill", "bill_found", "Final slip", "pass", f"{final['name']}: both weights and the net{note}",
                            final["answers"]["slip_kind"]["confidence"]))
    elif final:
        c = final["answers"]["slip_kind"]["confidence"]
        checks.append(check("bill", "bill_found", "Final slip", "review",
                            f"Only {c:.0%} sure {final['name']} is the final slip, with both weights and the net weight{note}. "
                            "Check it.", c, [final["name"]]))
    else:
        checks.append(check("bill", "bill_found", "Final slip", "review",
                            f"No slip clearly shows both weights and the net weight{note}, so the weighing times and net "
                            "weight couldn't be read. Look for the final slip.", images=named(slips), cause="no_final"))

    sure = [b for b in slips if b["answers"]["bill_number"]["confidence"] >= CONFIDENT]
    different = [b for b in sure if b["answers"]["bill_number"]["choice"] == "different"]
    unclear = [b for b in slips if b not in sure or b["answers"]["bill_number"]["choice"] == NOT_SHOWN]
    label = f"Bills match {vehicle_number}"
    if different:
        checks.append(check("bill", "bill_number", label, "fail",
                            f"{names(different)} {plural(different, 'shows', 'show')} a different vehicle number from "
                            f"{vehicle_number}.", images=named(different)))
    elif not slips:
        checks.append(check("bill", "bill_number", label, "review", f"No slip to compare with {vehicle_number}."))
    elif unclear:
        checks.append(check("bill", "bill_number", label, "review",
                            f"The vehicle number on {names(unclear)} couldn't be compared with {vehicle_number}. "
                            "Check the number on the slip.", images=named(unclear)))
    else:
        shown = f"All {len(slips)} slips show" if len(slips) > 1 else "The slip shows"
        checks.append(check("bill", "bill_number", label, "pass", f"{shown} {vehicle_number}",
                            min(b["answers"]["bill_number"]["confidence"] for b in slips)))
    checks.append(each_photo("bill", "bill_direct", "Bill taken directly", bills, "screen_photo"))

    if not quantity:
        checks.append(check("bill", "net_weight", "Net weight", "review", "No quantity was typed, so the net weight wasn't checked."))
    elif not final:
        checks.append(check("bill", "net_weight", f"Net weight is {quantity} kg", "review",
                            "No final slip to read the net weight from.", cause="no_final"))
    else:
        checks.append(choice_check("bill", "net_weight", "net_weight", final["answers"]["net_weight"], vehicle_number, quantity,
                                   final))
    # Only when Jev is sure either way: many slips don't label gross and tare (19 Oct 2026 run), and an
    # unsure answer there would send most trips to a person for nothing.
    if final and final["answers"]["gross_vs_tare"]["confidence"] >= CONFIDENT \
            and final["answers"]["gross_vs_tare"]["choice"] != NOT_SHOWN:
        checks.append(choice_check("bill", "gross_vs_tare", "gross_vs_tare", final["answers"]["gross_vs_tare"],
                                   vehicle_number, quantity, final))
    if weighbridge:
        checks.append(weighbridge_check(final, weighbridge))
    return checks + (verra_checks(final) if verra else [])


def weighbridge_check(final: dict | None, weighbridge: str, cause: str = "no_final") -> dict:
    """The final slip is from the weighbridge entered: a different one is a problem, unsure asks a person."""
    label = f"Slip from {weighbridge}"
    if not final:
        what = "No weigh bills" if cause == "bill_missing" else "No final slip"
        return check("bill", "weighbridge_matches", label, "review",
                     f"{what} to compare with the weighbridge entered, {weighbridge}.", cause=cause)
    answer = final["answers"].get("weighbridge_entered")
    if answer is None:  # answers saved before the question was asked
        return check("bill", "weighbridge_matches", label, "review",
                     f"{final['name']} wasn't asked about the weighbridge. Check it is from {weighbridge}.", images=[final["name"]])
    detail = {"same": f"{final['name']} is from {weighbridge}",
              "different": f"{final['name']} is from another weighbridge than {weighbridge}, the one entered. Check the "
                           "weighbridge entered, or the slip.",
              NOT_SHOWN: f"No weighbridge name could be read on {final['name']}. Check it is from {weighbridge}."}[answer["choice"]]
    status = {"same": "pass", "different": "fail", NOT_SHOWN: "review"}[answer["choice"]]
    if answer["confidence"] < CONFIDENT:
        status, detail = "review", unsure(detail, answer["confidence"]).split(". Check")[0] + \
            f". Compare the slip's heading with {weighbridge}."
    return check("bill", "weighbridge_matches", label, status, detail, answer["confidence"],
                 [final["name"]] if status != "pass" else [])


def photo_moments(results: list[dict]) -> list[tuple[dict, dict | None]]:
    """Each photo with its GPS stamp time as a moment, or None if it can't be read."""
    out = []
    for r in results:
        rd = {x["key"]: x for x in r["readings"]}
        out.append((r, moment(rd["photo_time"], rd["photo_date"])))
    return out


def busy_time(images: dict[str, list[dict]]) -> tuple[datetime, datetime] | None:
    """When the trip was out, by its photos' GPS stamps: from the first collection (loading) photo to the last
    unloading photo. None without a readable stamp on both kinds.

    Not the weighings: a vehicle is often weighed empty once in the morning and that tare printed on every slip of
    the day, so tare to gross would span all its earlier trips (19 and 23 Oct 2026).
    """
    loading = [m["at"] for _, m in photo_moments(images.get("loading", [])) if m]
    unloading = [m["at"] for _, m in photo_moments(images.get("unloading", [])) if m]
    if not loading or not unloading:
        return None
    times = loading + unloading
    return min(times), max(times)


def overlap_minutes(a: tuple[datetime, datetime], b: tuple[datetime, datetime]) -> float:
    """How long two spans of time overlap, in minutes; 0 or less if they don't."""
    return (min(a[1], b[1]) - max(a[0], b[0])).total_seconds() / 60


def photo_window(role: str, tare_first: bool) -> tuple[str | None, str | None]:
    """Which weighings a loading or unloading photo must fall between: (after, before), None where open.

    Tare first, loading is between tare and gross and unloading after gross. Gross first, loading is
    before gross and unloading between gross and tare.
    """
    if role == "loading":
        return ("tare", "gross") if tare_first else (None, "gross")
    return ("gross", None) if tare_first else ("gross", "tare")


def photo_timing(role: str, results: list[dict], w: dict, limits: Limits, tare_first: bool = True) -> dict | None:
    """Every loading or unloading photo in its window of the weighings (`photo_window`), give or take the clock slack.

    Unsure only because the weighing times are, it shares the weighing order's `cause`, "weigh_times".
    """
    if not results:
        return None
    slack = limits.clock_slack
    allowing = f" ({duration(slack)} clock difference allowed)" if slack else ""
    after, before = photo_window(role, tare_first)
    shown = dict(zip(("tare", "gross"), show(w["tare"], w["gross"])))
    plain = {"tare": f"the tare (empty) weighing at {shown['tare']}", "gross": f"the gross (loaded) weighing at {shown['gross']}"}
    word = "Loading" if role == "loading" else "Unloading"
    label = f"{word} photo times"
    taken = photo_moments(results)
    unread = [r for r, m in taken if not m]
    wrong, times = [], []
    for r, m in taken:
        if not m:
            continue
        early = bool(after) and minutes_between(w[after], m) < -slack
        late = bool(before) and minutes_between(m, w[before]) < -slack
        (wrong if early or late else times).append((r, m, early))
    weighed = w["certainty"] >= CONFIDENT
    unsure = [(r, m) for r, m in taken if m and m["certainty"] < CONFIDENT]
    sure_wrong = [x for x in wrong if weighed and x[1]["certainty"] >= CONFIDENT]
    at = lambda m: show(m, w["tare"], w["gross"])[0]
    window = (f"between {plain[after]} and {plain[before]}" if after and before
              else f"after {plain[after]}" if after else f"before {plain[before]}")
    if sure_wrong:
        r, m, early = sure_wrong[0]
        side = after if early else before
        also = f" So {plural(sure_wrong[1:], 'was', 'were')} {names([x[0] for x in sure_wrong[1:]])}." if sure_wrong[1:] else ""
        return check(role, f"{role}_timing", label, "fail",
                     f"{r['name']} was taken at {at(m)}, {'before' if early else 'after'} {plain[side]}. {word} photos must "
                     f"come {'after' if early else 'before'} it{allowing}.{also}",
                     min(m["certainty"], w["certainty"]), [x[0]["name"] for x in sure_wrong])
    if unread:
        return check(role, f"{role}_timing", label, "review",
                     f"The time on the GPS stamp of {names(unread)} couldn't be read. Check {plural(unread, 'it was', 'they were')} "
                     f"taken {window}.", images=named(unread))
    if unsure:
        photos = [r for r, _ in unsure]
        read = and_list(sorted({at(m) for _, m in unsure}))
        c = min(m["certainty"] for _, m in unsure)
        return check(role, f"{role}_timing", label, "review",
                     f"The time on the GPS stamp of {names(photos)} was read as {read}, but only {c:.0%} sure. "
                     f"Check {plural(photos, 'it was', 'they were')} taken {window}.", c, named(photos))
    short = (f"between {after} {shown[after]} and {before} {shown[before]}" if after and before
             else f"after {after} {shown[after]}" if after else f"before {before} {shown[before]}")
    if not weighed:
        if wrong:
            r, m, early = wrong[0]
            side = after if early else before
            text = f"{r['name']} at {at(m)} is {'before' if early else 'after'} {side} {shown[side]}"
        else:
            text = f"{word} {and_list(sorted({at(m) for _, m, _ in times}))} is {short}"
        return check(role, f"{role}_timing", label, "review", f"{text}, if the weighing times are right",
                     w["certainty"], cause="weigh_times")
    when = ", ".join(sorted({at(m) for _, m, _ in times}))
    return check(role, f"{role}_timing", label, "pass", f"{word} {when} is {short}",
                 min(min(m["certainty"] for _, m, _ in times), w["certainty"]))


def same_day(results: list[dict], w: dict) -> dict | None:
    """Every unloading photo dated the same day as the gross weighing (by the calendar date, so a trip
    weighed just before midnight and unloaded just after is flagged too)."""
    if not results:
        return None
    gross = w["gross"]
    label = "Unloading on the weighing day"
    taken = photo_moments(results)
    unread = [r for r, m in taken if not m]
    other_day = [(r, m) for r, m in taken if m and m["date"] != gross["date"]]
    sure = [(r, m) for r, m in other_day if min(m["certainty"], w["certainty"]) >= CONFIDENT]
    doubtful = [(r, m) for r, m in other_day if m["certainty"] < CONFIDENT]
    if sure:
        r, m = sure[0]
        return check("unloading", "unloading_day", label, "fail",
                     f"{r['name']} is dated {m['date']}, but the gross (loaded) weighing was on {gross['date']}. "
                     "Unloading must be on the same day.", min(m["certainty"], w["certainty"]), [r["name"] for r, _ in sure])
    if doubtful:
        photos = [r for r, _ in doubtful]
        c = min(m["certainty"] for _, m in doubtful)
        return check("unloading", "unloading_day", label, "review",
                     f"The date on the GPS stamp of {names(photos)} was read as {doubtful[0][1]['date']}, but only {c:.0%} sure; "
                     f"the gross (loaded) weighing was on {gross['date']}. Check the stamp date.", c, named(photos))
    if other_day:
        r, m = other_day[0]
        return check("unloading", "unloading_day", label, "review",
                     f"{r['name']} dated {m['date']}, gross on {gross['date']}, if the weighing date is right",
                     w["certainty"], cause="weigh_times")
    if unread:
        return check("unloading", "unloading_day", label, "review",
                     f"The date on the GPS stamp of {names(unread)} couldn't be read. Check unloading was on {gross['date']}, "
                     "the day of the gross (loaded) weighing.", images=named(unread))
    if w["certainty"] < CONFIDENT:
        # The gross date is as unsure as the weighing times: one cause with them, not a pass (27 Oct 2026).
        return check("unloading", "unloading_day", label, "review", f"All on {gross['date']}, if the weighing times are right",
                     w["certainty"], cause="weigh_times")
    return check("unloading", "unloading_day", label, "pass", f"All on {gross['date']}",
                 min(min(m["certainty"] for _, m in taken), w["certainty"]))


def trip_checks(images: dict[str, list[dict]], vehicle_number: str, quantity: str | None = None,
                limits: Limits = Limits(), plant: tuple[float, float] | None = None,
                verra: bool = False, weighbridge: str | None = None) -> tuple[list[dict], dict | None]:
    """Every check for a trip, and the weighings they used (None if the times couldn't be read).

    Tare first, the trip runs tare -> loading -> gross -> unloading: the weighings must be at least
    `min_gap_tare_first` apart, loading photos fall between them and unloading photos after gross.
    Gross first, it runs loading -> gross -> unloading -> tare: the weighings must be at least
    `min_gap_gross_first` apart, loading photos fall before gross and unloading photos between them.
    Photos may be off by `clock_slack`, and unloading must be on the same day as the gross weighing.
    With `plant`, unloading must be stamped within `plant_radius` of it. With `verra` (a Verra project
    trip), the final slip must have a material entered and carry the weighbridge's stamp. With `weighbridge`
    (the one entered), the final slip must be from it.
    """
    checks = photo_checks("loading", images.get("loading", []), vehicle_number)
    checks += photo_checks("unloading", images.get("unloading", []), vehicle_number)
    if plant and images.get("unloading"):
        checks.append(plant_check(images["unloading"], plant, limits))
    bills = images.get("bill", [])
    checks += bill_checks(bills, vehicle_number, quantity, verra, weighbridge)

    final = final_slip(bills)
    w = weighings(final, first_slip(bills)) if final else None
    gap = minutes_between(w["tare"], w["gross"]) if w else None  # positive when the tare was taken first
    not_checked = "so the weighing order and photo times weren't checked"
    if not w:
        if final:
            detail = (f"The tare (empty) and gross (loaded) times on {final['name']} couldn't be read, {not_checked}. "
                      "Read the times on the slip.")
        else:
            detail = f"No final slip to read the weighing times from, {not_checked}." if bills else f"No weigh bills, {not_checked}."
        checks.append(check("times", "weigh_order", "Weighing order", "review", detail, images=[final["name"]] if final else [],
                            cause=None if final else "no_final" if bills else "bill_missing"))
        return checks, None
    g, t = show(w["gross"], w["tare"])
    if gap == 0:
        checks.append(check("times", "weigh_order", "Weighing order", "review",
                            f"{and_list(w['slips'])} {plural(w['slips'], 'shows', 'show')} the tare (empty) and gross (loaded) "
                            f"weighings both at {g}. Check the times on the slip.", images=w["slips"]))
        return checks, w
    tare_first = gap > 0
    # An unsure reading of the weighing times also makes the gap and the photo times unsure: one cause, one look.
    cause = "weigh_times" if w["certainty"] < CONFIDENT else None
    if cause:
        order = check("times", "weigh_order", "Weighing order", "review",
                      f"Check the weighing times on {and_list(w['slips'])}: read as tare (empty) {t} and gross (loaded) {g}, "
                      f"from {w['source']}, but only {w['certainty']:.0%} sure.", w["certainty"], w["slips"], cause)
    else:
        first = f"Tare first ({t}), then gross ({g})" if tare_first else f"Gross first ({g}), then tare ({t})"
        order = check("times", "weigh_order", "Weighing order", "pass", f"{first}, from {w['source']}", w["certainty"])
    checks.append({**order, "order": "tare_first" if tare_first else "gross_first"})
    if w["source"].startswith("Time In"):
        # Time In should be the tare (19 Oct 2026: on every In/Out slip). The heavier weight at Time In means the
        # details were entered swapped, or the vehicle was weighed loaded first: either way a person looks.
        first = final["answers"]["first_weight"]
        label = "Time In is the empty weighing"
        if first["choice"] == "gross":
            swapped = (f"the heavier weight is at Time In ({g}). Either In and Out were entered swapped, "
                       "or the vehicle was weighed loaded first")
            if first["confidence"] >= CONFIDENT:
                status, detail = "fail", f"{final['name']}: {swapped}."
            else:
                status, detail = "review", f"{final['name']}: {unsure(swapped, first['confidence'])}. Check which weight is at Time In."
            checks.append(check("times", "time_in_tare", label, status, detail, first["confidence"], [final["name"]]))
        else:
            checks.append(check("times", "time_in_tare", label, "pass",
                                f"{final['name']}: the lighter weight is at Time In ({t})", first["confidence"]))
    apart, least = abs(gap), limits.min_gap_tare_first if tare_first else limits.min_gap_gross_first
    between = "loading" if tare_first else "unloading"
    if cause:
        status = "review"
        detail = f"{'Only ' if apart < least else ''}{duration(apart)} apart if those times are right (at least {duration(least)} needed)"
    elif apart >= least:
        status, detail = "pass", f"{duration(apart)} apart (at least {duration(least)})"
    else:
        status = "fail"
        detail = (f"The tare (empty) and gross (loaded) weighings are only {duration(apart)} apart, but at least "
                  f"{duration(least)} is needed for {between} between them.")
    checks.append(check("times", "weigh_gap", "Time between weighings", status, detail, w["certainty"],
                        w["slips"] if status == "fail" else [], cause))
    # A tare from another day, more than `max_overnight_gap` before or after the gross, is a stored one, not
    # weighed on this trip (27 Oct 2026: 172907, 172940, 173097). Nearer, the trip ran across midnight.
    days, overnight = (w["tare"]["date"], w["gross"]["date"]), apart <= limits.max_overnight_gap
    if cause:
        status = "review"
        detail = (f"Both on {days[1]}" if days[0] == days[1] else f"Tare on {days[0]}, gross on {days[1]}") + \
            ", if the weighing times are right"
    elif days[0] == days[1]:
        status, detail = "pass", f"Both on {days[1]}"
    elif overnight:
        status, detail = "pass", f"Across midnight, {duration(apart)} apart (at most {duration(limits.max_overnight_gap)})"
    else:
        status = "fail"
        detail = (f"The tare (empty) weighing on {and_list(w['slips'])} is dated {days[0]}, but the gross (loaded) weighing "
                  f"{days[1]}, {duration(apart)} apart. The tare may be a stored one, not weighed on this trip: check the vehicle was weighed "
                  "empty for this trip.")
    checks.append(check("times", "weigh_day", "Tare weighed on this trip", status, detail, w["certainty"],
                        w["slips"] if status == "fail" else [], cause))
    for role in ("loading", "unloading"):
        if c := photo_timing(role, images.get(role, []), w, limits, tare_first):
            checks.append(c)
    if c := same_day(images.get("unloading", []), w):
        checks.append(c)
    return checks, w


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
        return check("trips", "reused", label, "fail",
                     describe(seen[True]) + ". The same image can't be proof for two trips.", images=list(seen[True]))
    if seen[False]:
        return check("trips", "reused", label, "review",
                     "Looks the same as an image in another trip: " + describe(seen[False]) + ". Compare them.",
                     images=list(seen[False]))
    return check("trips", "reused", label, "pass", "None seen in another trip")


def overlap_checks(trips: dict[str, dict], slack: int = Limits().clock_slack) -> dict[str, dict]:
    """A vehicle can't be on two trips at once: {trip: check} for each trip whose window (its photos' `busy_time`)
    overlaps another trip's with the same vehicle number by more than `slack` minutes (the cameras' clocks).
    `trips` maps a trip id to {"vehicle_number", "verdict"}.
    """
    windows = {}
    for trip, t in trips.items():
        if w := t["verdict"].get("window"):
            windows[trip] = (t["vehicle_number"], datetime.fromisoformat(w[0]), datetime.fromisoformat(w[1]))
    found = {}
    for a, (va, sa, ea) in windows.items():
        for b, (vb, sb, eb) in windows.items():
            if a < b and va == vb and overlap_minutes((sa, ea), (sb, eb)) > slack:
                for x, y in ((a, b), (b, a)):
                    found.setdefault(x, []).append(y)
    return {trip: check("trips", "overlap", "One trip at a time", "fail",
                        f"{trips[trip]['vehicle_number']} is also on trip {and_list(sorted(others))} at the same time, by "
                        "the photos' stamps. A vehicle can't be on two trips at once.")
            for trip, others in found.items()}
