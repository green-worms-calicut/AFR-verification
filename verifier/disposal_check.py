"""A disposal request checked with proof_check, and its flags as gw_sales stores them.

The request's photos are fetched by s3Key and checked as one trip: weigh bills, collection photos (loading) and
unloading photos, against the vehicle number and net quantity entered, with the facility's location as the
plant. A photo that isn't in storage, or isn't an image, asks a person (`image_unavailable`, docs part). So does an
image Jev still couldn't answer for on the check's last try, while the other images' checks are sent; when Jev was
busy or down for an image before that, the check is tried again (`JevUnavailable`), asking only what wasn't answered.
"""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime

from typesafe_sdk import TypeSafeClient

import proof_check
from proof_check import Upload
from verifier import compare_disposals, entry_checks, s3_photos, standards

log = logging.getLogger("verifier")

# The contract's evidence categories as proof_check's kinds of image. gps_photo and verification are
# never sent; anything else is skipped and logged.
KINDS = {"weighbill": "bill", "collection": "loading", "unloading": "unloading"}
WORDS = {"bill": "weigh bill", "loading": "collection photo", "unloading": "unloading photo"}
MISSING_KEY = {"bill": "bill_missing", "loading": "loading_missing", "unloading": "unloading_missing"}
# A flag's severity by its check's part and status: what is wrong with a real trip is high, everything else a
# recheck, but only information when a Verra check is unsure.
SEVERITY = {("real", "fail"): "high", ("real", "review"): "recheck",
            ("docs", "fail"): "recheck", ("docs", "review"): "recheck",
            ("verra", "fail"): "recheck", ("verra", "review"): "info"}
RANK = {"high": 0, "recheck": 1, "info": 2}
KIND = {"fail": "problem found", "review": "please check"}
JEV_COULDNT = "couldn't be read by Jev"  # why an image is unavailable when Jev didn't answer for it
MAX_MESSAGE = 2000  # the contract's limit


class JevUnavailable(Exception):
    """Jev was busy or down for some of the disposal's images: worth trying again."""


@dataclass
class Disposal:
    """What proof_check needs from a request."""

    vehicle_number: str
    quantity: str | None  # net kg, as entered; None when entered in bags
    plant: tuple[float, float] | None  # the facility's location
    bad_plant: tuple[float, float] | None  # the facility's location when it isn't in India: not checked against
    verra: bool
    day: date  # the trip's date in IST, which picks the standard
    evidence: dict[str, list[str]]  # s3Keys by kind of image
    vehicle_key: str | None  # the vehicle number cleaned up, to find its other disposals; None if it isn't one
    weighbridge: str | None  # the weighbridge's name entered, which the final slip must be from
    quantity_key: str | None  # the quantity as entered, kept with the request (comparisons don't filter by it)


@dataclass
class Checked:
    outcome: str  # passed or flagged
    flags: list[dict]
    extractions: dict
    standard: dict
    analysis: dict  # proof_check's whole result, kept by us
    cost_usd: float


def disposal(request: dict, verra_project: str | None = None) -> Disposal:
    claims = request.get("claims") or {}
    reference = request.get("reference") or {}
    typed = str((claims.get("vehicle") or {}).get("vehicleNumber") or "")
    quantity = None
    unit = (claims.get("unit") or "kg").lower()
    quantity_key = None if claims.get("quantity") is None else f"{claims['quantity']} {unit}"
    if claims.get("quantity") is not None and unit in ("kg", "kgs"):
        try:
            quantity = proof_check.parse_quantity(str(claims["quantity"]))
        except ValueError:  # 0 or not a number: nothing to match the slip's net weight against
            log.warning("%s: quantity %r not checked", request["requestId"], claims["quantity"])
    location = (reference.get("facility") or {}).get("location") or {}
    plant = (location["lat"], location["lng"]) if location.get("lat") is not None and location.get("lng") is not None else None
    bad_plant = plant if plant and not proof_check.in_india(*plant) else None
    # Verra when any attached log's client is on the Verra project (a mix shouldn't happen; if it does, check it too).
    projects = {((entry.get("client") or {}).get("defaultProject") or {}).get("code")
                for entry in reference.get("collectionLogs") or []}
    evidence = {kind: [] for kind in WORDS}
    for item in request.get("evidence") or []:
        kind = KINDS.get(item.get("category"))
        if kind is None:
            log.info("%s: skipped evidence %s of category %r", request["requestId"], item.get("s3Key"), item.get("category"))
            continue
        if item.get("s3Key") not in evidence[kind]:
            evidence[kind].append(item["s3Key"])
    day = (standards.ist_day(claims.get("arrivalTime")) or standards.ist_day((request.get("subject") or {}).get("createdAt"))
           or datetime.now(standards.IST).date())
    vehicle = proof_check.normalize_vehicle_number(typed)
    weighbridge = str((claims.get("weighbridge") or {}).get("name") or "").strip() or None
    return Disposal(vehicle or typed.strip() or "(not entered)", quantity, None if bad_plant else plant, bad_plant,
                    bool(verra_project) and verra_project in projects, day, evidence, vehicle, weighbridge,
                    f"{quantity} kg" if quantity else quantity_key)


def bad_plant_check(plant: tuple[float, float], unloading: list[dict]) -> dict:
    """The facility's location entered isn't in India, so no unloading photo could be checked against it."""
    return proof_check.check("unloading", "unloading_plant", "At the plant", "review",
                             f"The facility's location in gw_sales, {plant[0]}, {plant[1]}, isn't in India (latitude and "
                             "longitude may be swapped), so the unloading photos weren't checked against it: fix it in "
                             "gw_sales and check the photos' GPS stamps.", images=[r["name"] for r in unloading])


def unavailable_check(kind: str, found: list[tuple[str, str]], none_left: bool) -> dict:
    """The images of one kind the request lists that can't be checked, as (s3Key, why). When no image of the kind
    is left, it shares the missing kind's cause, so it leads one flag with the checks that leaves unsure."""
    detail = " ".join(f"The {WORDS[kind]} {key} {why}." for key, why in found)
    stored = [key for key, why in found if why != JEV_COULDNT]
    them = "it" if len(stored) == 1 else "them"
    unread = "it" if len(found) - len(stored) == 1 else "them"
    tail = (f" Find {them} in gw_sales and add {them} again." if stored else "") + \
        (f" Look at {unread} yourself." if len(stored) < len(found) else "")
    return proof_check.check(kind, "image_unavailable", f"{WORDS[kind].capitalize()}s available", "review",
                             detail + tail, images=[key for key, _ in found],
                             cause=MISSING_KEY[kind] if none_left else None)


def flags_of(checks: list[dict]) -> list[dict]:
    """One flag per cause: checks flagged for the same reason (proof_check's `cause`) together, led by a problem
    if there is one, naming the others it affects. Problems first, then by severity."""
    groups = []
    for c in checks:
        if c["status"] == "pass":
            continue
        same = c["cause"] and next((g for g in groups if g[0]["cause"] == c["cause"]), None)
        if same:
            same.append(c)
        else:
            groups.append([c])
    made = [flag(g) for g in groups]
    made.sort(key=lambda f: (f["kind"] != KIND["fail"], RANK[f["severity"]]))
    return made


def flag(group: list[dict]) -> dict:
    lead = next((c for c in group if c["status"] == "fail"), group[0])
    also = [c for c in group if c is not lead]
    status = "fail" if any(c["status"] == "fail" for c in group) else "review"
    message = lead["detail"]
    if also:
        message += " It also affects: " + proof_check.and_list([c["label"] for c in also]) + "."
    made = {"code": lead["key"], "severity": min((SEVERITY[c["part"], c["status"]] for c in group), key=RANK.get),
            "kind": KIND[status], "message": message[:MAX_MESSAGE],
            "evidence": list(dict.fromkeys(i for c in [lead, *also] for i in c["images"])),
            "checks": [c["key"] for c in [lead, *also]]}
    if others := compare_disposals.related(x["id"] for c in group for x in c.get("related", [])):
        made["related"] = others
    if len({json.dumps(c.get("target"), sort_keys=True) for c in group}) == 1 and lead.get("target"):
        made["target"] = lead["target"]  # the collection log it is about, when every check in it is about the same one
    sure = [c["certainty"] for c in group if c["certainty"] is not None]
    if sure:
        made["confidence"] = round(min(sure), 3)
    return made


def extractions(trip: dict, unavailable: list[str]) -> dict:
    """What was read, for gw_sales to store and show: no weights (Jev only judges them against the entry)."""
    v = trip["verdict"]
    return {
        "parts": {p: part["status"] for p, part in v["parts"].items()},
        "timeline": v["timeline"],
        "weighingWindow": v["window"],
        "images": {r["name"]: {"kind": kind, "readings": {x["key"]: {"value": x["value"], "confidence": round(x["min_confidence"], 3)}
                                                          for x in r["readings"]}}
                   for kind, results in trip["images"].items() for r in results},
        "unavailable": unavailable,
    }


def check(request: dict, fetch: Callable[[str], bytes | None], client: TypeSafeClient,
          verra_project: str | None = None, others: list[compare_disposals.Other] = (), cache=None,
          last_try: bool = False) -> Checked:
    """Check one request's disposal, and compare it with `others`: the same vehicle's other disposals within a day
    of its date (`database.others`). `cache` holds the request's Jev answers from earlier tries. S3 failing raises,
    and so does Jev being busy or down for an image (`JevUnavailable`), so the job retries; on the `last_try` such an
    image is flagged and the rest sent, unless Jev answered for none."""
    d = disposal(request, verra_project)
    std = standards.for_day(d.day)
    uploads, unavailable = {kind: [] for kind in WORDS}, []  # (kind, s3Key, why)
    for kind, keys in d.evidence.items():
        for key in keys:
            if not key.startswith(s3_photos.PHOTO_PREFIXES):
                unavailable.append((kind, key, s3_photos.OUTSIDE))
                continue
            data = fetch(key)
            if why := s3_photos.problem(data):
                unavailable.append((kind, key, why))
            else:
                uploads[kind].append(Upload(key, data))
    trip, _ = proof_check.analyze_trip(client, uploads, d.vehicle_number, standards.MODEL, d.quantity, std.limits,
                                       plant=d.plant, verra=d.verra, box_model=standards.BOX_MODEL,
                                       weighbridge=d.weighbridge, place_also=entry_checks.photos_to_place(request),
                                       cache=cache, around=d.day)
    busy = [u["name"] for u in trip["unanswered"] if u["kind"] == "retry"]
    if busy and not last_try:
        raise JevUnavailable(f"Jev didn't answer for {len(busy)} image(s)")
    if trip["unanswered"] and not any(trip["images"].values()):
        raise JevUnavailable("Jev answered for none of the images")
    unavailable += [(u["role"], u["name"], JEV_COULDNT) for u in trip["unanswered"]]
    # Read the same in gw_sales as a photo missing from storage: unavailable_check below, not image_unanswered.
    trip["verdict"]["checks"] = [c for c in trip["verdict"]["checks"] if c["key"] != "image_unanswered"]
    # A kind whose every image is unavailable wasn't left out: its missing check would say none were added.
    gone = {kind for kind, _, _ in unavailable if not trip["images"][kind]}
    by_kind = {}
    for kind, key, why in unavailable:
        by_kind.setdefault(kind, []).append((key, why))
    # First, so each leads the flag of the checks its missing images leave unsure.
    checks = [unavailable_check(kind, found, kind in gone) for kind, found in by_kind.items()]
    checks += [c for c in trip["verdict"]["checks"] if c["key"] not in {MISSING_KEY[k] for k in gone}]
    if d.bad_plant:
        checks.append(bad_plant_check(d.bad_plant, trip["images"]["unloading"]))
    checks += entry_checks.checks(request, trip["images"], std.collection_radius, d.day)
    checks += compare_disposals.checks(d.vehicle_key, trip, list(others), std.limits.clock_slack, d.day)
    trip["verdict"] = {**trip["verdict"], **proof_check.decide_parts(checks)}
    for kind, key, why in unavailable:
        log.warning("%s: %s %s %s", request["requestId"], WORDS[kind], key, why)
    made = flags_of(checks)
    cost = sum(r["cost_usd"] for results in trip["images"].values() for r in results)
    found = {**extractions(trip, [key for _, key, _ in unavailable]),
             "collectedVsWeighed": entry_checks.collected_vs_weighed(request)}
    return Checked("flagged" if made else "passed", made, found,
                   {**std.describe(), "plant": d.plant is not None, "verra": d.verra}, trip, cost)
