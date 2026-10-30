"""Checks across disposals, from what was kept when each was checked (its latest proof_check result).

Only disposals of the same vehicle within a day of this trip's date are looked at, never this disposal itself
(its earlier requests are edits), whatever their quantity: photos borrowed from another trip come with a different
one (23 Oct 2026: 173186's collection photos were 172929's unloading photos). An image also in such a disposal is a
problem if an exact copy and asks a person if a look-alike (`reused`). The trip's time out, from its first
collection photo to its last unloading photo, overlapping another disposal's by more than the clock slack is a
problem (`overlap`): not the weighings, as one morning tare is often printed on every slip of the day. A photo
stamped within SAME_MOMENT minutes of one in another disposal asks a person (`same_moment`).
Each other disposal is flagged once, by the first of these that finds it. Every flag names the other disposals in
`related`. (One file as both a collection and an unloading photo of the disposal itself is proof_check's `same_file`.)
"""

from dataclasses import dataclass

import proof_check
from proof_check import Limits, and_list, busy_time, check, overlap_minutes, unsure_stamps

SAME_MOMENT = 5  # minutes between photo stamps in two disposals that make them look like one moment
PHOTOS = ("loading", "unloading")


@dataclass
class Other:
    """Another disposal, as last checked."""

    disposal_id: int
    analysis: dict  # proof_check's result: {"images": {kind: [result]}, "verdict": ...}


def related(ids) -> list[dict]:
    return [{"type": "disposal_log", "id": i} for i in sorted(set(ids))]


def disposals(ids) -> str:
    ids = sorted(set(ids))
    return ("disposal " if len(ids) == 1 else "disposals ") + and_list([str(i) for i in ids])


def reused(images: dict[str, list[dict]], others: list[Other]) -> dict:
    """Images also in another disposal: an exact copy is a problem, a look-alike asks a person."""
    index, of = proof_check.ImageIndex(), {}
    for o in others:
        for results in o.analysis["images"].values():
            for r in results:
                index.add(str(o.disposal_id), r["name"], r["prints"])
                of[r["name"]] = o.disposal_id
    exact, alike = [], []
    for results in images.values():
        for r in results:
            for m in index.matches(r["prints"], None):
                (exact if m["exact"] else alike).append((r["name"], m["name"], int(m["trip"])))
    label = "Images not used in other disposals"
    if not exact and not alike:
        return check("trips", "reused", label, "pass", "None seen in another disposal of this vehicle")
    found = exact or alike
    what = "the same file as" if exact else "looks the same as"
    detail = "; ".join(f"{a} is {what} {b} in disposal {i}" for a, b, i in dict.fromkeys(found))
    tail = " The same photo can't be proof for two disposals." if exact else " Compare them."
    made = check("trips", "reused", label, "fail" if exact else "review", detail + "." + tail,
                 images=list(dict.fromkeys(n for a, b, _ in found for n in (a, b))))
    made["related"] = related(i for _, _, i in found)
    return made


def moments(images: dict[str, list[dict]], around=None) -> list[tuple[dict, dict]]:
    """Each collection and unloading photo with its readable stamp time."""
    return [(r, m) for kind in PHOTOS for r, m in proof_check.photo_moments(images.get(kind, []), around) if m]


def same_moment(images: dict[str, list[dict]], others: list[Other], flagged: set[int], around=None) -> dict:
    """A photo stamped within SAME_MOMENT minutes of a photo in another disposal, one not already `flagged` by
    `reused` or `overlap`."""
    found = []
    for r, m in moments(images, around):
        for o in others:
            if o.disposal_id in flagged:
                continue
            for r2, m2 in moments(o.analysis["images"], around):
                if abs((m["at"] - m2["at"]).total_seconds()) <= SAME_MOMENT * 60:
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


def overlap(vehicle: str, images: dict[str, list[dict]], others: list[Other], flagged: set[int], slack: int,
            around=None) -> dict | None:
    """The vehicle can't be on two trips at once: this trip's time out (`busy_time`) overlapping another disposal's by
    more than `slack` minutes, one not already `flagged` by `reused`. A problem only by stamps Jev was sure of on both
    sides; by unsure ones too, it asks a person. None without the photos to time it."""
    here, here_sure = busy_time(images, around=around), busy_time(images, sure_only=True, around=around)
    if not here:
        return None
    found, doubtful = [], []
    for o in others:
        theirs = o.analysis["images"]
        if o.disposal_id in flagged or not (w := busy_time(theirs, around=around)) or overlap_minutes(here, w) <= slack:
            continue
        sure = busy_time(theirs, sure_only=True, around=around)
        if here_sure and sure and overlap_minutes(here_sure, sure) > slack:
            found.append((o.disposal_id, sure))
        else:
            doubtful.append(o)
    label = "One trip at a time"
    if not found and doubtful:
        stamps = [(name, m, None) for name, m in unsure_stamps(images, around)]
        stamps += [(name, m, o.disposal_id) for o in doubtful for name, m in unsure_stamps(o.analysis["images"], around)]
        read = "; ".join(f"{name}{f' in disposal {i}' if i else ''} was read as {m['time']} on {m['date']}, but only "
                         f"{m['certainty']:.0%} sure" for name, m, i in stamps)
        made = check("trips", "overlap", label, "review",
                     f"{vehicle}'s photos for this disposal would overlap {disposals(o.disposal_id for o in doubtful)} "
                     f"by stamps Jev wasn't sure of: {read}. Check those stamp times.",
                     min(m["certainty"] for _, m, _ in stamps), images=[name for name, _, i in stamps if i is None])
        made["related"] = related(o.disposal_id for o in doubtful)
        return made
    if not found:
        return check("trips", "overlap", label, "pass", "No other disposal of this vehicle while its photos were taken")
    at = "; ".join(f"disposal {i}'s {w[0]:%H:%M}-{w[1]:%H:%M} on {w[0]:%d/%m}" for i, w in found)
    here = here_sure
    made = check("trips", "overlap", label, "fail",
                 f"{vehicle}'s photos for this disposal were taken {here[0]:%H:%M}-{here[1]:%H:%M} on {here[0]:%d/%m}, "
                 f"overlapping {at}. A vehicle can't be on two trips at once: check which trip is real.")
    made["related"] = related(i for i, _ in found)
    return made


def checks(vehicle: str | None, analysis: dict, others: list[Other], slack: int = Limits().clock_slack,
           around=None) -> list[dict]:
    """Every check across disposals for one checked disposal, `slack` the minutes cameras' clocks may differ.
    Without a vehicle number, only one saying it wasn't compared. Each other disposal is flagged by the first check
    that finds it."""
    images = analysis["images"]
    if not vehicle:
        return [check("trips", "not_compared", "Compared with other disposals", "review",
                      "The vehicle number entered couldn't be read as a number, so this disposal wasn't compared with "
                      "other disposals. Check it against the photos.")]
    made = []
    reuse = reused(images, others)
    made.append(reuse)
    flagged = {r["id"] for r in reuse.get("related", [])}
    if c := overlap(vehicle, images, others, flagged, slack, around):
        made.append(c)
        flagged |= {r["id"] for r in c.get("related", [])}
    made.append(same_moment(images, others, flagged, around))
    return made
