"""Checks across disposals, from what was kept when each was checked (its latest proof_check result).

Only disposals of the same vehicle within a day of this trip's date are looked at, never this disposal itself
(its earlier requests are edits). Images are compared only with those also of the same quantity: an exact copy
is a problem and a look-alike asks a person (`reused`); a photo stamped within SAME_MOMENT minutes of one in
such a disposal asks a person (`same_moment`). Weighings overlapping another disposal's, of any quantity, are a
problem (`overlap`). Within the disposal, one file as both a collection and an unloading photo is a problem
(`same_file`). Every flag names the other disposals in `related`.
"""

from dataclasses import dataclass
from datetime import datetime

import proof_check
from proof_check import and_list, check

SAME_MOMENT = 5  # minutes between photo stamps in two disposals that make them look like one moment
PHOTOS = ("loading", "unloading")


@dataclass
class Other:
    """Another disposal, as last checked."""

    disposal_id: int
    quantity: str | None
    analysis: dict  # proof_check's result: {"images": {kind: [result]}, "verdict": ...}


def related(ids) -> list[dict]:
    return [{"type": "disposal_log", "id": i} for i in sorted(set(ids))]


def disposals(ids) -> str:
    ids = sorted(set(ids))
    return ("disposal " if len(ids) == 1 else "disposals ") + and_list([str(i) for i in ids])


def same_file(images: dict[str, list[dict]]) -> dict:
    """One file can't be both a collection (loading) photo and an unloading photo of the trip."""
    loading = {r["prints"]["md5"]: r["name"] for r in images.get("loading", [])}
    both = [(loading[r["prints"]["md5"]], r["name"]) for r in images.get("unloading", []) if r["prints"]["md5"] in loading]
    label = "Different photos for loading and unloading"
    if not both:
        return check("trips", "same_file", label, "pass", "No photo is both a collection and an unloading photo")
    pairs = "; ".join(a if a == b else f"{a} and {b}" for a, b in both)
    return check("trips", "same_file", label, "fail",
                 f"The same file is both a collection photo and an unloading photo: {pairs}. One photo can't show both "
                 "the loading and the unloading.", images=list(dict.fromkeys(n for pair in both for n in pair)))


def reused(images: dict[str, list[dict]], others: list[Other]) -> tuple[dict, set[tuple[str, int]]]:
    """Images also in another disposal: the check, and the (this image, other disposal) pairs found."""
    index, of = proof_check.ImageIndex(), {}
    for o in others:
        for results in o.analysis["images"].values():
            for r in results:
                index.add(str(o.disposal_id), r["name"], r["prints"])
                of[r["name"]] = o.disposal_id
    exact, alike, pairs = [], [], set()
    for results in images.values():
        for r in results:
            for m in index.matches(r["prints"], None):
                (exact if m["exact"] else alike).append((r["name"], m["name"], int(m["trip"])))
                pairs.add((r["name"], int(m["trip"])))
    label = "Images not used in other disposals"
    if not exact and not alike:
        return check("trips", "reused", label, "pass", "None seen in another disposal of this vehicle"), pairs
    found = exact or alike
    what = "the same file as" if exact else "looks the same as"
    detail = "; ".join(f"{a} is {what} {b} in disposal {i}" for a, b, i in dict.fromkeys(found))
    tail = " The same photo can't be proof for two disposals." if exact else " Compare them."
    made = check("trips", "reused", label, "fail" if exact else "review", detail + "." + tail,
                 images=list(dict.fromkeys(n for a, b, _ in found for n in (a, b))))
    made["related"] = related(i for _, _, i in found)
    return made, pairs


def moments(images: dict[str, list[dict]]) -> list[tuple[dict, dict]]:
    """Each collection and unloading photo with its readable stamp time."""
    return [(r, m) for kind in PHOTOS for r, m in proof_check.photo_moments(images.get(kind, [])) if m]


def same_moment(images: dict[str, list[dict]], others: list[Other], copies: set[tuple[str, int]]) -> dict:
    """A photo stamped within SAME_MOMENT minutes of a photo in another disposal. A photo already found to be a copy
    of one in that disposal is `reused`'s, so isn't compared with it again."""
    found = []
    for r, m in moments(images):
        for o in others:
            for r2, m2 in moments(o.analysis["images"]):
                if (r["name"], o.disposal_id) not in copies and abs((m["at"] - m2["at"]).total_seconds()) <= SAME_MOMENT * 60:
                    found.append((r["name"], m, r2["name"], m2, o.disposal_id))
    label = "Photos not from another disposal's moment"
    if not found:
        return check("trips", "same_moment", label, "pass", f"No photo within {SAME_MOMENT} min of one in another disposal")
    detail = "; ".join(f"{a} was stamped {m['time']} on {m['date']}, {b} in disposal {i} {m2['time']} on {m2['date']}"
                       for a, m, b, m2, i in found)
    made = check("trips", "same_moment", label, "review",
                 f"{detail}. Photos of one vehicle within {SAME_MOMENT} min in two disposals may be one trip entered "
                 "twice: check they're different trips.", min(min(m["certainty"], m2["certainty"]) for _, m, _, m2, _ in found),
                 images=list(dict.fromkeys(n for a, _, b, _, _ in found for n in (a, b))))
    made["related"] = related(i for *_, i in found)
    return made


def window(analysis: dict) -> tuple[datetime, datetime] | None:
    w = analysis["verdict"].get("window")
    return (datetime.fromisoformat(w[0]), datetime.fromisoformat(w[1])) if w else None


def overlap(vehicle: str, analysis: dict, others: list[Other]) -> dict | None:
    """The vehicle can't be on two trips at once: this trip's weighings overlapping another disposal's.

    None when this trip's weighing order isn't clear (its own checks already ask a person about the times).
    """
    here = window(analysis)
    if not here:
        return None
    found = [(o.disposal_id, w) for o in others if (w := window(o.analysis)) and here[0] < w[1] and w[0] < here[1]]
    label = "One trip at a time"
    if not found:
        return check("trips", "overlap", label, "pass", "No other disposal of this vehicle between its weighings")
    at = "; ".join(f"disposal {i} {w[0]:%H:%M}-{w[1]:%H:%M} on {w[0]:%d/%m}" for i, w in found)
    made = check("trips", "overlap", label, "fail",
                 f"{vehicle} is weighed {here[0]:%H:%M}-{here[1]:%H:%M} on {here[0]:%d/%m} for this disposal, overlapping "
                 f"{at}. A vehicle can't be on two trips at once.")
    made["related"] = related(i for i, _ in found)
    return made


def checks(vehicle: str | None, quantity: str | None, analysis: dict, others: list[Other]) -> list[dict]:
    """Every check across disposals for one checked disposal. Without a vehicle number, only `same_file`."""
    made = [same_file(analysis["images"])]
    if not vehicle:
        return made
    alike = [o for o in others if o.quantity == quantity]
    reuse, copies = reused(analysis["images"], alike)
    made += [reuse, same_moment(analysis["images"], alike, copies)]
    if c := overlap(vehicle, analysis, others):
        made.append(c)
    return made
