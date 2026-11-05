"""Checks across disposals, from what was kept when each was checked (its latest proof_check result).

An exact copy of an image is found in any other disposal, of any vehicle and date, from the fingerprints kept with
every request (`database.Copy`), at most MOST_NAMED disposals named. Look-alikes and the timing comparisons look only
at the vehicle's disposals with a trip within a day of this trip's date. Never this disposal itself (its earlier
requests are edits), whatever the quantity: photos borrowed from another trip come with a different one (23 Oct
2026: 173186's collection photos were 172929's unloading photos). An image also in another disposal is a problem if
an exact copy and asks a person if a look-alike (`reused`). The trip's time out, from its first
collection photo (or without one its gross weighing) to its last unloading photo, overlapping another disposal's by
more than the clock slack is a problem (`overlap`): not the tare, as one morning tare is often printed on every slip
of the day. A photo
stamped within SAME_MOMENT minutes of one in another disposal asks a person (`same_moment`). The same gross
weighing, within SAME_WEIGHING minutes, on the final slips of two disposals is a problem (`same_weighing`): one
weighing can't be proof for two disposals.
Each other disposal is flagged once, by the first of these that finds it. Every flag names the other disposals in
`related`. (One file as both a collection and an unloading photo of the disposal itself is proof_check's `same_file`.)
"""

from dataclasses import dataclass
from datetime import date

import proof_check
from proof_check import CONFIDENT, Limits, and_list, busy_time, check, overlap_minutes, trip_weighings, unsure_stamps
from verifier.database import Copy

SAME_WEIGHING = 2  # minutes between the gross weighings on two disposals' slips that make them one weighing
MOST_NAMED = 5  # other disposals named for exact copies found in all history; the rest are counted
SAME_MOMENT = 5  # minutes between photo stamps in two disposals that make them look like one moment
PHOTOS = ("loading", "unloading")


@dataclass
class Other:
    """Another disposal, as last checked."""

    disposal_id: int
    analysis: dict  # proof_check's result: {"images": {kind: [result]}, "verdict": ...}
    quantity: str | None = None  # the quantity entered, as kept with its request, e.g. "2570 kg"


def related(ids) -> list[dict]:
    return [{"type": "disposal_log", "id": i} for i in sorted(set(ids))]


def disposals(ids) -> str:
    ids = sorted(set(ids))
    return ("disposal " if len(ids) == 1 else "disposals ") + and_list([str(i) for i in ids])


def where(copy: Copy | None) -> str:
    """The vehicle and date of a copy's disposal, as ' (KL07BC1234, 01/09)': only what is known."""
    known = [x for x in (copy and copy.vehicle, copy and copy.day and f"{copy.day:%d/%m}") if x]
    return f" ({', '.join(known)})" if known else ""


def as_named(key: str, disposal: int | None = None) -> str:
    """An image's name in a message: its s3Key, unless the caller names images for people (`disposal_check.Namer`).
    `disposal` is the other disposal it is in, None for this one's."""
    return key


def reused(images: dict[str, list[dict]], others: list[Other], copies: list[Copy] = (), name=as_named) -> dict:
    """Images also in another disposal: an exact copy is a problem, a look-alike asks a person. Exact copies are
    found in `copies` (any vehicle, any date) and in `others`; look-alikes only in `others`. `name` names an image in
    the message (`as_named`)."""
    index = proof_check.ImageIndex()
    for o in others:
        for results in o.analysis["images"].values():
            for r in results:
                index.add(str(o.disposal_id), r["name"], r["prints"])
    by_md5 = {}
    for c in copies:
        by_md5.setdefault(c.md5, []).append(c)
    exact, alike, at, history = [], [], {}, []
    for results in images.values():
        for r in results:
            for m in index.matches(r["prints"], None):
                (exact if m["exact"] else alike).append((r["name"], m["name"], int(m["trip"])))
            for c in by_md5.get(r["prints"]["md5"], []):
                history.append((r["name"], c.name, c.disposal_id))
                at[c.disposal_id, c.name] = c
    # Copies in this vehicle's nearby disposals are all named; the others from history at most MOST_NAMED, the latest.
    near = {i for _, _, i in exact}
    far = sorted({i for _, _, i in history} - near,
                 key=lambda i: (min((c.day or date.min for (d, _), c in at.items() if d == i), default=date.min), i),
                 reverse=True)
    named = set(far[:MOST_NAMED])
    more = len(far) - len(named)
    exact = list(dict.fromkeys(exact + [h for h in history if h[2] in near | named]))
    label = "Images not used in other disposals"
    if not exact and not alike:
        return check("trips", "reused", label, "pass", "None seen in another disposal")
    found = exact or alike
    what = "the same file as" if exact else "looks the same as"
    detail = "; ".join(f"{name(a)} is {what} {name(b, i)} in disposal {i}{where(at.get((i, b)))}"
                       for a, b, i in dict.fromkeys(found))
    if more:
        detail += f"; and {more} more disposals"
    tail = " The same photo can't be proof for two disposals." if exact else " Compare them."
    made = check("trips", "reused", label, "fail" if exact else "review", detail + "." + tail,
                 images=list(dict.fromkeys(n for a, b, _ in found for n in (a, b))))
    made["related"] = related(i for _, _, i in found)
    return made


def moments(images: dict[str, list[dict]], around=None) -> list[tuple[dict, dict]]:
    """Each collection and unloading photo with its readable stamp time."""
    return [(r, m) for kind in PHOTOS for r, m in proof_check.photo_moments(images.get(kind, []), around) if m]


def same_moment(images: dict[str, list[dict]], others: list[Other], flagged: set[int], around=None,
                name=as_named) -> dict:
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
    detail = "; ".join(f"{name(a)} was stamped {m['time']} on {m['date']}, {name(b, i)} in disposal {i} "
                       f"{m2['time']} on {m2['date']}" for a, m, b, m2, i in found)
    made = check("trips", "same_moment", label, "review",
                 f"{detail}. Photos of one vehicle within {SAME_MOMENT} min in two disposals may be one trip entered "
                 "twice: check they're different trips.", min(min(m["certainty"], m2["certainty"]) for _, m, _, m2, _ in found),
                 images=list(dict.fromkeys(n for a, _, b, _, _ in found for n in (a, b))))
    made["related"] = related(i for *_, i in found)
    return made


def same_weighing(images: dict[str, list[dict]], quantity: str | None, others: list[Other], flagged: set[int],
                  around=None, name=as_named) -> dict | None:
    """The same gross weighing, within SAME_WEIGHING minutes, on this trip's final slip and another disposal's, one not
    already `flagged` by `reused`: a problem when Jev was sure of both times, else it asks a person. The tare isn't
    compared, as one morning tare is printed on every slip of the day. None without a gross weighing to compare."""
    w = trip_weighings(images, around)
    if not w:
        return None
    here = w["gross"]
    found = []
    for o in others:
        if o.disposal_id in flagged or not (theirs := trip_weighings(o.analysis["images"], around)):
            continue
        there = theirs["gross"]
        if here["date"] == there["date"] and abs((here["at"] - there["at"]).total_seconds()) <= SAME_WEIGHING * 60:
            found.append((o, theirs))
    label = "Weighing not in another disposal"
    if not found:
        return check("trips", "same_weighing", label, "pass", "No other disposal of this vehicle with the same gross weighing")
    sure_enough = lambda t: here["certainty"] >= CONFIDENT and t["gross"]["certainty"] >= CONFIDENT
    sure = [(o, t) for o, t in found if sure_enough(t)]
    doubtful = [(o, t) for o, t in found if not sure_enough(t)]
    ours = w["slips"][-1]
    final = proof_check.final_slip(images["bill"])
    parts = []
    for o, t in sure or found:
        part = (f"{name(ours)} shows the gross weighing at {here['time']} on {here['date']}, the same as "
                f"{disposals([o.disposal_id])}'s {name(t['slips'][-1], o.disposal_id)}")
        theirs_final = proof_check.final_slip(o.analysis["images"]["bill"])
        net = lambda slip: slip["answers"].get("net_weight", {}).get("choice")  # asked only of a quantity in kg
        if (quantity and quantity.endswith(" kg") and quantity == o.quantity and net(final) == "same"
                and net(theirs_final) == "same"):
            part += f", and the same net weight, {quantity.split()[0]} kg"
        parts.append(part)
    detail = "; ".join(parts)
    if sure:
        also = (f" It may also be the same weighing as {disposals(o.disposal_id for o, _ in doubtful)}, by times Jev "
                "wasn't sure of: check those too.") if doubtful else ""
        made = check("trips", "same_weighing", label, "fail",
                     f"{detail}. One weighing can't be proof for two disposals.{also}",
                     images=list(dict.fromkeys([ours, *(t["slips"][-1] for _, t in sure)])))
    else:
        unclear = [(ours, here, None)] + [(t["slips"][-1], t["gross"], o.disposal_id) for o, t in found]
        read = "; ".join(f"{name(key, i)}{f' in disposal {i}' if i else ''} was read as {m['time']} on {m['date']}, "
                         f"but only {m['certainty']:.0%} sure" for key, m, i in unclear if m["certainty"] < CONFIDENT)
        made = check("trips", "same_weighing", label, "review",
                     f"{detail}, but Jev wasn't sure of every time: {read}. Check the gross time on both slips.",
                     min(m["certainty"] for _, m, _ in unclear),
                     images=list(dict.fromkeys(key for key, _, _ in unclear)))
    made["related"] = related(o.disposal_id for o, _ in found)
    return made


def overlap(vehicle: str, images: dict[str, list[dict]], others: list[Other], flagged: set[int], slack: int,
            around=None, name=as_named) -> dict | None:
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
        stamps = [(key, m, None) for key, m in unsure_stamps(images, around)]
        stamps += [(key, m, o.disposal_id) for o in doubtful for key, m in unsure_stamps(o.analysis["images"], around)]
        read = "; ".join(f"{name(key, i)}{f' in disposal {i}' if i else ''} was read as {m['time']} on {m['date']}, "
                         f"but only {m['certainty']:.0%} sure" for key, m, i in stamps)
        made = check("trips", "overlap", label, "review",
                     f"{vehicle}'s photos for this disposal would overlap {disposals(o.disposal_id for o in doubtful)} "
                     f"by stamps Jev wasn't sure of: {read}. Check those stamp times.",
                     min(m["certainty"] for _, m, _ in stamps), images=[key for key, _, i in stamps if i is None])
        made["related"] = related(o.disposal_id for o in doubtful)
        return made
    if not found:
        return check("trips", "overlap", label, "pass", "No other disposal of this vehicle while its photos were taken")
    at = "; ".join(f"disposal {i}'s {w[0]:%H:%M}-{w[1]:%H:%M} on {w[0]:%d/%m}" for i, w in found)
    here = here_sure
    # Without a collection photo the trip starts at the gross weighing (`proof_check.gross_start`).
    by, were = ("photos", "were taken") if images.get("loading") else ("gross weighing and unloading photos", "were at")
    also = (f" It may also overlap {disposals(o.disposal_id for o in doubtful)}, by stamps Jev wasn't sure of: check "
            "those too.") if doubtful else ""
    made = check("trips", "overlap", label, "fail",
                 f"{vehicle}'s {by} for this disposal {were} {here[0]:%H:%M}-{here[1]:%H:%M} on {here[0]:%d/%m}, "
                 f"overlapping {at}. A vehicle can't be on two trips at once: check which trip is real.{also}")
    made["related"] = related([*(i for i, _ in found), *(o.disposal_id for o in doubtful)])
    return made


def checks(vehicle: str | None, analysis: dict, others: list[Other], slack: int = Limits().clock_slack,
           around=None, quantity: str | None = None, copies: list[Copy] = (), name=as_named) -> list[dict]:
    """Every check across disposals for one checked disposal, `slack` the minutes cameras' clocks may differ.
    Without a vehicle number, only `reused` (exact copies in `copies` need none) and one saying it wasn't compared. Each other disposal is flagged by the first check
    that finds it. `name` names images in the messages (`as_named`)."""
    images = analysis["images"]
    if not vehicle:
        return [reused(images, [], copies, name),
                check("trips", "not_compared", "Compared with other disposals", "review",
                      "The vehicle number entered couldn't be read as a number, so this disposal wasn't compared with "
                      "other disposals. Check it against the photos.")]
    made = []
    reuse = reused(images, others, copies, name)
    made.append(reuse)
    flagged = {r["id"] for r in reuse.get("related", [])}
    if c := same_weighing(images, quantity, others, flagged, around, name):
        made.append(c)
        flagged |= {r["id"] for r in c.get("related", [])}
    if c := overlap(vehicle, images, others, flagged, slack, around, name):
        made.append(c)
        flagged |= {r["id"] for r in c.get("related", [])}
    made.append(same_moment(images, others, flagged, around, name))
    return made
