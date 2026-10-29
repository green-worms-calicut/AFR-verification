"""A disposal request checked with check_proof, and its flags as gw_sales stores them.

The request's photos are fetched by s3Key and checked as one trip: weigh bills, collection photos (loading) and
unloading photos, against the vehicle number and net quantity entered, with the facility's location as the
plant. A photo that isn't in storage, or isn't an image, asks a person (`image_unavailable`, docs part).
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime

from typesafe_sdk import TypeSafeClient

import check_proof
from check_proof import Upload
from verifier import photos, standard

log = logging.getLogger("verifier")

# The contract's evidence categories as check_proof's kinds of image. gps_photo and verification are
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
MAX_MESSAGE = 2000  # the contract's limit


@dataclass
class Disposal:
    """What check_proof needs from a request."""

    vehicle_number: str
    quantity: str | None  # net kg, as entered; None when entered in bags
    plant: tuple[float, float] | None  # the facility's location
    verra: bool
    day: date  # the trip's date in IST, which picks the standard
    evidence: dict[str, list[str]]  # s3Keys by kind of image


@dataclass
class Checked:
    outcome: str  # passed or flagged
    flags: list[dict]
    extractions: dict
    standard: dict
    analysis: dict  # check_proof's whole result, kept by us
    cost_usd: float


def disposal(request: dict, verra_project: str | None = None) -> Disposal:
    claims = request.get("claims") or {}
    reference = request.get("reference") or {}
    typed = str((claims.get("vehicle") or {}).get("vehicleNumber") or "")
    quantity = None
    if claims.get("quantity") is not None and (claims.get("unit") or "kg").lower() in ("kg", "kgs"):
        try:
            quantity = check_proof.parse_quantity(str(claims["quantity"]))
        except ValueError:  # 0 or not a number: nothing to match the slip's net weight against
            log.warning("%s: quantity %r not checked", request["requestId"], claims["quantity"])
    location = (reference.get("facility") or {}).get("location") or {}
    plant = (location["lat"], location["lng"]) if location.get("lat") is not None and location.get("lng") is not None else None
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
    day = (standard.ist_day(claims.get("arrivalTime")) or standard.ist_day((request.get("subject") or {}).get("createdAt"))
           or datetime.now(standard.IST).date())
    return Disposal(check_proof.normalize_vehicle_number(typed) or typed.strip() or "(not entered)", quantity, plant,
                    bool(verra_project) and verra_project in projects, day, evidence)


def unavailable_check(kind: str, found: list[tuple[str, str]], none_left: bool) -> dict:
    """The images of one kind the request lists that can't be checked, as (s3Key, why). When no image of the kind
    is left, it shares the missing kind's cause, so it leads one flag with the checks that leaves unsure."""
    detail = " ".join(f"The {WORDS[kind]} {key} {why}." for key, why in found)
    them = "it" if len(found) == 1 else "them"
    return check_proof.check(kind, "image_unavailable", f"{WORDS[kind].capitalize()}s available", "review",
                             f"{detail} Find {them} in gw_sales and add {them} again.", images=[key for key, _ in found],
                             cause=MISSING_KEY[kind] if none_left else None)


def flags_of(checks: list[dict]) -> list[dict]:
    """One flag per cause: checks flagged for the same reason (check_proof's `cause`) together, led by a problem
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
        message += " It also affects: " + check_proof.and_list([c["label"] for c in also]) + "."
    made = {"code": lead["key"], "severity": min((SEVERITY[c["part"], c["status"]] for c in group), key=RANK.get),
            "kind": KIND[status], "message": message[:MAX_MESSAGE],
            "evidence": list(dict.fromkeys(i for c in [lead, *also] for i in c["images"])),
            "checks": [c["key"] for c in [lead, *also]]}
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
          verra_project: str | None = None) -> Checked:
    """Check one request's disposal. Jev or S3 failing raises, so the job retries."""
    d = disposal(request, verra_project)
    std = standard.for_day(d.day)
    uploads, unavailable = {kind: [] for kind in WORDS}, []  # (kind, s3Key, why)
    for kind, keys in d.evidence.items():
        for key in keys:
            data = fetch(key)
            if why := photos.problem(data):
                unavailable.append((kind, key, why))
            else:
                uploads[kind].append(Upload(key, data))
    trip, _ = check_proof.analyze_trip(client, uploads, d.vehicle_number, standard.MODEL, d.quantity, std.limits,
                                       plant=d.plant, verra=d.verra, box_model=standard.BOX_MODEL)
    # A kind whose every image is unavailable wasn't left out: its missing check would say none were added.
    gone = {kind for kind, _, _ in unavailable if not uploads[kind]}
    by_kind = {}
    for kind, key, why in unavailable:
        by_kind.setdefault(kind, []).append((key, why))
    # First, so each leads the flag of the checks its missing images leave unsure.
    checks = [unavailable_check(kind, found, kind in gone) for kind, found in by_kind.items()]
    checks += [c for c in trip["verdict"]["checks"] if c["key"] not in {MISSING_KEY[k] for k in gone}]
    trip["verdict"] = {**trip["verdict"], **check_proof.decide_parts(checks)}
    for kind, key, why in unavailable:
        log.warning("%s: %s %s %s", request["requestId"], WORDS[kind], key, why)
    made = flags_of(checks)
    cost = sum(r["cost_usd"] for results in trip["images"].values() for r in results)
    return Checked("flagged" if made else "passed", made, extractions(trip, [key for _, key, _ in unavailable]),
                   {**std.describe(), "plant": d.plant is not None, "verra": d.verra}, trip, cost)
