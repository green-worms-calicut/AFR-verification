"""A replayed day's report: what the verifier would have sent for each disposal, ordered for review, with the marks
a person gives it.

Flagged disposals come first, then the risky passes: passes that only just cleared their threshold (`barely`), least
sure first, then a random sample of the other passes (the same for a day every time, `sample`), then the rest. Marks
say whether a flag was right or wrong, or that a passed check should have been flagged (missed); they are kept by
disposal and check, so they outlast a recheck, and the report says which of them a recheck changed.
"""

import base64
import io
import json
import random
from collections import Counter
from datetime import date
from pathlib import Path

import proof_check
from proof_check.trip_rules import CONFIDENT, NO, NOUL_CHECKS, NOUL_THRESHOLDS, YES
from tools.check_trip_folders import compare
from tools.replay_day import files
from verifier import disposal_check, standards

MARGIN = 0.10  # a pass this close above its threshold is barely passed
SAMPLE = 5  # other passed disposals picked at random each day
WORDS = {"bill": "weigh bill", "loading": "collection photo", "unloading": "unloading photo"}
DISPOSAL_MARK = "disposal"  # check_key of "reviewed, nothing missed" on a passed disposal


def flag_about(flag: dict) -> str:
    """What tells a flag apart from another of the same check in one disposal: the log or disposals it is about."""
    found = []
    if target := flag.get("target"):
        found.append(f"{target.get('type')}:{target.get('id')}")
    found += [f"{r.get('type')}:{r.get('id')}" for r in flag.get("related") or []]
    return " ".join(found)


def close_call(check: dict, slot: str | None, margin: float) -> str | None:
    """How a passed check only just cleared its threshold ("same, 0.84, needs 0.80"), or None if it cleared it well.
    `slot` is the image's (photo or bill) for a yes/no answer of one image."""
    certainty = check.get("certainty")
    if check["status"] != "pass" or certainty is None:
        return None
    if check.get("measure") == "yes":
        high, low = NOUL_THRESHOLDS.get((slot, check["key"]), (YES, NO))
        if NOUL_CHECKS.get(check["key"], (None,) * 4)[3] is False:  # passes on "no": sure as far as yes is low
            return f"no, {1 - certainty:.2f} sure, needs {1 - low:.2f}" if certainty > low - margin else None
        return f"yes, {certainty:.2f} sure, needs {high:.2f}" if certainty < high + margin else None
    return f"{certainty:.2f} sure, needs {CONFIDENT:.2f}" if certainty < CONFIDENT + margin else None


def close_calls(analysis: dict, names: dict[str, str], margin: float) -> list[dict]:
    """A disposal's passed checks that only just cleared their thresholds, least sure first: the trip's own checks
    and each image's answers."""
    found = []
    for c in analysis["verdict"]["checks"]:
        if why := close_call(c, None, margin):
            found.append({"key": c["key"], "label": c["label"], "why": why, "sure": c["certainty"], "images": c["images"]})
    for kind, results in analysis["images"].items():
        for r in results:
            for c in r.get("checks", []):
                if why := close_call(c, r.get("slot"), margin):
                    sure = 1 - c["certainty"] if NOUL_CHECKS.get(c["key"], (None,) * 4)[3] is False else c["certainty"]
                    found.append({"key": c["key"], "label": f"{c['label']} ({names.get(r['name'], r['name'])})",
                                  "why": why, "sure": sure, "images": [r["name"]]})
    return sorted(found, key=lambda x: x["sure"])


def image_list(body: dict) -> list[dict]:
    return [{"key": item["s3Key"], "name": item.get("fileName") or item["s3Key"].rsplit("/", 1)[-1],
             "kind": disposal_check.KINDS.get(item.get("category"), item.get("category")),
             "file": files.file_name(item["s3Key"])}
            for item in body.get("evidence") or [] if item.get("s3Key")]


def ist(text: str | None) -> str | None:
    at = standards.ist_time(text)
    return f"{at:%d %b %H:%M}" if at else None


def truth_of(day: date, trips: Path) -> dict[int, dict]:
    """Hand-read values by disposal id, from the day folder's truth.jsonl, if there is one."""
    path = files.day_folder(day, trips) / "truth.jsonl"
    if not path.is_file():
        return {}
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return {int(r["disposal"]): r for r in rows}


def truth_items(truth: dict, analysis: dict, vehicle: str, keys: dict[str, str]) -> list[dict]:
    """Jev's readings against a person's, with the image names (file names in truth.jsonl) as s3Keys."""
    by_key = lambda found: {keys.get(name, name): value for name, value in (found or {}).items()}
    t = {"case": None, **truth, "photos": by_key(truth.get("photos")), "bills": by_key(truth.get("bills"))}
    return compare(analysis, t, {"vehicle_number": vehicle})


def build(day: date, bodies: list[dict], saved: dict[int, dict], marks: dict[tuple[int, str, str], dict],
          margin: float = MARGIN, sample: int = SAMPLE, trips: Path = files.TRIPS) -> dict:
    """The report's data: `bodies` are the day's disposals in order of entry, `saved` their results by disposal id
    (`checking.results`), `marks` every mark."""
    truth = truth_of(day, trips)
    cards, labels = [], {}
    for body in bodies:
        disposal_id = body["subject"]["id"]
        claims, images = body.get("claims") or {}, image_list(body)
        names = {i["key"]: i["name"] for i in images}
        card = {"id": disposal_id, "vehicle": str((claims.get("vehicle") or {}).get("vehicleNumber") or ""),
                "quantity": f"{claims.get('quantity')} {claims.get('unit') or 'kg'}" if claims.get("quantity") is not None else None,
                "facility": (claims.get("facility") or {}).get("name"), "weighbridge": (claims.get("weighbridge") or {}).get("name"),
                "createdVia": body["subject"].get("createdVia"), "entered": ist(body["subject"].get("createdAt")),
                "arrival": ist(claims.get("arrivalTime")), "images": images, "flags": [], "checks": [], "risky": [],
                "timeline": [], "parts": {}, "truth": [], "notChecked": not_checked(body, day, trips)}
        found = saved.get(disposal_id)
        if not found:
            card["outcome"] = "unchecked"
        elif found["result"]["outcome"] == "error":
            card["outcome"], card["error"] = "error", (found["result"].get("error") or {}).get("message")
        else:
            result, analysis = found["result"], found["analysis"]
            card |= {"outcome": result["outcome"], "cost": found["cost"], "verra": (result.get("standard") or {}).get("verra"),
                     "parts": (result.get("extractions") or {}).get("parts", {}),
                     "timeline": analysis["verdict"].get("timeline", [])}
            for f in result["flags"]:
                about = flag_about(f)
                card["flags"].append({**f, "about": about, "images": [{"key": k, "name": names.get(k, k)} for k in f.get("evidence", [])],
                                      **marks.get((disposal_id, f["code"], about), {})})
            for c in analysis["verdict"]["checks"]:
                labels.setdefault(c["key"], c["label"])
                about = f"pass:{c.get('group') or ''}"
                card["checks"].append({"key": c["key"], "label": c["label"], "status": c["status"], "detail": c["detail"],
                                       "certainty": c.get("certainty"), "part": c.get("part"), "about": about,
                                       **(marks.get((disposal_id, c["key"], about), {}) if c["status"] == "pass" else {})})
            card["risky"] = close_calls(analysis, names, margin)
            if disposal_id in truth:
                d = disposal_check.disposal(body)
                card["truth"] = truth_items(truth[disposal_id], analysis, d.vehicle_number,
                                            {i["name"]: i["key"] for i in images})
        card["reviewed"] = reviewed(card, marks)
        card["disposalMark"] = marks.get((disposal_id, DISPOSAL_MARK, ""), {}).get("mark")
        cards.append(card)
    order(cards, day, sample)
    return {"day": day.isoformat(), "dayName": files.day_name(day), "margin": margin, "sample": sample,
            "summary": summary(cards), "accuracy": accuracy(cards, marks, labels), "changes": changes(cards, marks),
            "duplicates": duplicates(cards), "disposals": cards}


def not_checked(body: dict, day: date, trips: Path) -> list[str]:
    """What the verifier couldn't check for a disposal because the export lacks it."""
    reference = body.get("reference") or {}
    location = (reference.get("facility") or {}).get("location") or {}
    found = []
    if location.get("lat") is None or location.get("lng") is None:
        found.append("facility has no location: unloading photos not checked against it")
    if not reference.get("collectionLogs"):
        found.append("no collection logs: pickup checks skipped")
    elif not any(((e.get("collectionPoint") or {}).get("location") or {}).get("lat") is not None
                 for e in reference["collectionLogs"]):
        found.append("no collection point location: collection photos not checked against it")
    if not (body.get("claims") or {}).get("weighbridge"):
        found.append("no weighbridge entered: slip's weighbridge not checked")
    if files.missing_images(day, [body], trips):
        found.append("images not downloaded")
    return found


def reviewed(card: dict, marks: dict) -> bool:
    """Every flag marked, or, for a passed disposal, marked as reviewed with nothing missed (or a miss marked)."""
    if card["outcome"] == "flagged":
        return all(f.get("mark") for f in card["flags"])
    if card["outcome"] == "passed":
        return (card["id"], DISPOSAL_MARK, "") in marks or any(c.get("mark") for c in card["checks"])
    return False


def order(cards: list[dict], day: date, sample: int) -> None:
    """Put each card in its group, in review order: flagged, barely passed, sampled, passed, error, unchecked."""
    passed = [c for c in cards if c["outcome"] == "passed"]
    barely = sorted((c for c in passed if c["risky"]), key=lambda c: c["risky"][0]["sure"])
    rest = [c for c in passed if not c["risky"]]
    picked = set(c["id"] for c in random.Random(day.isoformat()).sample(rest, min(sample, len(rest))))
    for c in cards:
        c["group"] = ("barely" if c["risky"] else "sample" if c["id"] in picked else "passed") if c["outcome"] == "passed" \
            else c["outcome"]
    rank = {"flagged": 0, "barely": 1, "sample": 2, "passed": 3, "error": 4, "unchecked": 5}
    first = {c["id"]: i for i, c in enumerate(barely)}
    cards.sort(key=lambda c: (rank[c["group"]], first.get(c["id"], 0)))


def summary(cards: list[dict]) -> dict:
    flags = [f for c in cards for f in c["flags"]]
    not_checked = Counter(why for c in cards for why in c["notChecked"])
    return {"disposals": len(cards), "outcomes": dict(Counter(c["outcome"] for c in cards)),
            "cost": round(sum(c.get("cost") or 0 for c in cards), 4),
            "bySeverity": dict(Counter(f["severity"] for f in flags)),
            "byCheck": dict(Counter(f["code"] for f in flags).most_common()),
            "notChecked": dict(not_checked.most_common()),
            "reviewed": sum(1 for c in cards if c["reviewed"]),
            "toReview": sum(1 for c in cards if c["outcome"] in ("flagged", "passed")),
            "riskyPasses": sum(1 for c in cards if c["group"] == "barely")}


def accuracy(cards: list[dict], marks: dict, labels: dict[str, str]) -> list[dict]:
    """By check: flags marked right or wrong and passes marked missed, over every replayed day, and how many of this
    day's flags are unmarked."""
    rows = {}
    for (_, key, _), m in marks.items():
        if key == DISPOSAL_MARK:
            continue
        rows.setdefault(key, Counter())[m["mark"]] += 1
    for c in cards:
        for f in c["flags"]:
            if not f.get("mark"):
                rows.setdefault(f["code"], Counter())["unmarked"] += 1
    return sorted(({"check": k, "label": labels.get(k, k), "right": n["right"], "wrong": n["wrong"], "missed": n["missed"],
                    "unmarked": n["unmarked"]} for k, n in rows.items()), key=lambda r: (-(r["wrong"] + r["missed"]), r["check"]))


def changes(cards: list[dict], marks: dict) -> list[dict]:
    """Marks a recheck has changed the meaning of: a flag marked wrong that is gone (fixed), one marked right that is
    gone (a new miss), and a miss now flagged (fixed)."""
    by_id = {c["id"]: c for c in cards}
    found = []
    for (disposal_id, key, about), m in marks.items():
        card = by_id.get(disposal_id)
        if not card or card["outcome"] not in ("passed", "flagged") or key == DISPOSAL_MARK:
            continue
        if m["mark"] in ("right", "wrong") and not any(f["code"] == key and f["about"] == about for f in card["flags"]):
            found.append({"disposal": disposal_id, "check": key, "fixed": m["mark"] == "wrong",
                          "what": "marked wrong, no longer flagged" if m["mark"] == "wrong" else "marked right, no longer flagged"})
        elif m["mark"] == "missed" and any(f["code"] == key or key in f.get("checks", []) for f in card["flags"]):
            found.append({"disposal": disposal_id, "check": key, "fixed": True, "what": "marked missed, now flagged"})
    return found


def duplicates(cards: list[dict]) -> list[dict]:
    """Every flag linking a disposal with another: (this disposal, the others, the flag)."""
    return [{"disposal": c["id"], "others": [r["id"] for r in f["related"]], "code": f["code"], "severity": f["severity"],
             "message": f["message"], "images": f["images"]}
            for c in cards for f in c["flags"] if f.get("related")]


def thumbnail(path: Path, longest: int = 480) -> bytes | None:
    """A saved image made small and upright, as JPEG; None if it can't be read."""
    try:
        img = proof_check.load_bytes(path.read_bytes())
    except (OSError, *proof_check.UNREADABLE):
        return None
    img.thumbnail((longest, longest))
    buffer = io.BytesIO()
    img.convert("RGB").save(buffer, "JPEG", quality=72)
    return buffer.getvalue()


def embed_images(report: dict, day: date, trips: Path = files.TRIPS) -> None:
    """Put each image's thumbnail in the report as a data URL, for the saved copy (no server behind it)."""
    folder = files.day_folder(day, trips)
    for card in report["disposals"]:
        for image in card["images"]:
            small = thumbnail(folder / str(card["id"]) / image["file"], 360)
            image["data"] = "data:image/jpeg;base64," + base64.b64encode(small).decode("ascii") if small else None


PAGE = Path(__file__).with_name("page.html")


def static_page(report: dict) -> str:
    """The page with the report in it, read-only."""
    data = json.dumps(report, default=str).replace("</", "<\\/")
    return PAGE.read_text().replace("<script id=\"report-data\"></script>",
                                    f"<script id=\"report-data\">window.REPORT = {data};</script>")
