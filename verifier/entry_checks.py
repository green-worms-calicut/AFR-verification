"""Checks of what was entered in gw_sales against the photos: every collection log has a photo, every collection
photo was taken on its pickup day and near its collection point, no unloading photo long after the end time, and
the collections before the disposal's arrival. Also what was collected by hand scale against the net weight entered: recorded, not flagged.

gw_sales sends times in UTC and the photos' GPS stamps show the phone's time, IST, so every time entered is turned
into IST first (`standards.ist_time`). A stamp is read as a day and month without a year, so dates are compared by
day and month.
"""

from datetime import datetime, timedelta

import proof_check
from proof_check import and_list, check, photo_moments
from verifier import compare_disposals, standards

LATE_AFTER_END = timedelta(hours=1, minutes=30)  # how long after the end time entered an unloading photo may be
MANUAL = "MANUAL_TRIP_ENTRY"  # entered after the fact: its times can be hours or days off, so only its dates count


def logs(request: dict) -> list[dict]:
    return (request.get("reference") or {}).get("collectionLogs") or []


def owners(request: dict) -> dict[str, list[int | None]]:
    """Each collection photo's s3Key and every collection log's id it is listed under (distinct, in order), with
    None for a photo of the disposal itself."""
    found: dict[str, list[int | None]] = {}
    for item in request.get("evidence") or []:
        if item.get("category") == "collection" and item.get("s3Key"):
            owner = item.get("owner") or {}
            log_id = owner.get("id") if owner.get("type") == "collection_log" else None
            if log_id not in found.setdefault(item["s3Key"], []):
                found[item["s3Key"]].append(log_id)
    return found


def pickup(entry: dict) -> datetime | None:
    """A collection log's pickup (its collectionDate) in IST."""
    return standards.ist_time(entry.get("collectionDate"))


def log_name(entry: dict, picked_up: bool = False) -> str:
    """'collection log 45001 (ABC Hotel, Main Kitchen)', with ', picked up 30/10' inside the brackets if `picked_up`
    and the pickup is known."""
    where = [x for x in ((entry.get("client") or {}).get("name"), (entry.get("collectionPoint") or {}).get("name")) if x]
    if picked_up and (at := pickup(entry)):
        where.append(f"picked up {at:%d/%m}")
    return f"collection log {entry['id']}" + (f" ({', '.join(where)})" if where else "")


def has_photos(request: dict) -> list[dict]:
    """Every collection log has a photo of its own: one check per log without, aimed at that log."""
    entries = logs(request)
    if not entries:
        return []
    taken = {i for ids in owners(request).values() for i in ids if i is not None}
    label = "Every collection log has a photo"
    missing = [e for e in entries if e["id"] not in taken]
    if not missing:
        return [check("collection", "collection_has_photos", label, "pass", f"Each of the {len(entries)} logs has a photo")]
    made = []
    for e in missing:
        c = check("collection", "collection_has_photos", label, "review",
                  f"{log_name(e)[0].upper()}{log_name(e)[1:]} has no collection photo: ask for the photo taken at the pickup.")
        c["target"] = {"type": "collection_log", "id": e["id"]}
        made.append(c)
    return made


def log_same_photo(request: dict, images: dict[str, list[dict]]) -> list[dict]:
    """Each collection log has a photo of its own: one photo listed under two logs, the same file under two keys in
    different logs, or a look-alike in different logs, is not one pickup's photo of its own place. The same file
    twice in one log only asks a person. One pass check, or one check per finding; none without logs."""
    entries = {e["id"]: e for e in logs(request)}
    if not entries:
        return []
    label = "Each collection log has its own photo"
    named = lambda i: log_name(entries[i]) if i in entries else f"collection log {i}"
    short = lambda i: named(i).removeprefix("collection log ")
    owner_of = {k: [i for i in ids if i is not None] for k, ids in owners(request).items()}
    prints = {r["name"]: r["prints"] for r in images.get("loading", []) if r["name"] in owner_of and r.get("prints")}
    findings, seen = [], []

    def found(status: str, detail: str, keys: list[str], target: int | None = None) -> None:
        findings.append((status, detail, keys, target))

    for key, ids in owner_of.items():
        if len(ids) > 1:
            found("fail", f"{key} is the photo of both {named(ids[0])} and {' and '.join(short(i) for i in ids[1:])}. "
                  "Each pickup needs its own photo.", [key])
    by_md5: dict[str, list[str]] = {}
    for key, p in prints.items():
        by_md5.setdefault(p["md5"], []).append(key)
    for keys in by_md5.values():
        for n, a in enumerate(keys):
            for b in keys[n + 1:]:
                seen.append(frozenset((a, b)))
                apart = [(x, y) for x in owner_of[a] for y in owner_of[b] if x != y]
                if apart:
                    x, y = apart[0]
                    found("fail", f"{a} and {b} are the same file, the photo of {named(x)} and of {short(y)}. "
                          "Each pickup needs its own photo.", [a, b])
                elif set(owner_of[a]) & set(owner_of[b]):
                    x = next(i for i in owner_of[a] if i in owner_of[b])
                    found("review", f"{a} and {b} are the same file, both under {named(x)}. Check the log has a photo of "
                          "its own pickup.", [a, b], x)
    index = proof_check.ImageIndex()
    for key, p in prints.items():
        for i in owner_of[key]:
            index.add(str(i), key, p)
    for key, p in prints.items():
        for i in owner_of[key]:
            for m in index.matches(p, str(i)):
                pair = frozenset((key, m["name"]))
                if m["exact"] or m["name"] == key or pair in seen:
                    continue
                seen.append(pair)
                found("review", f"{key} ({named(i)}) and {m['name']} ({named(int(m['trip']))}) look the same. Compare them.",
                      [key, m["name"]])
    if not findings:
        return [check("collection", "log_same_photo", label, "pass", f"Each of the {len(entries)} logs has its own photo")]
    # One check per shared photo file: findings that name a common file are one, a problem if any part is.
    groups: list[list[tuple]] = []
    for finding in findings:
        joined = [g for g in groups if any(set(finding[2]) & set(f[2]) for f in g)]
        merged = [finding] + [f for g in joined for f in g]
        groups = [g for g in groups if g not in joined] + [sorted(merged, key=findings.index)]
    made = []
    for group in sorted(groups, key=lambda g: findings.index(g[0])):
        status = "fail" if any(f[0] == "fail" for f in group) else "review"
        c = check("collection", "log_same_photo", label, status, " ".join(f[1] for f in group),
                  images=list(dict.fromkeys(k for f in group for k in f[2])))
        if len(group) == 1 and group[0][3] is not None:
            c["target"] = {"type": "collection_log", "id": group[0][3]}
        made.append(c)
    return made


def photo_date(request: dict, images: dict[str, list[dict]], around=None) -> dict | None:
    """Every collection photo stamped on its log's pickup day; a photo of the disposal itself, on the pickup day of
    one of its logs. None when no photo has a readable stamp and a pickup to compare with."""
    pickups = {e["id"]: (e, at) for e in logs(request) if (at := pickup(e))}
    if not pickups:
        return None
    owner_of = owners(request)
    wrong, checked = [], 0
    for r, m in photo_moments(images.get("loading", []), around):
        if m is None or r["name"] not in owner_of:  # an unreadable stamp is the GPS stamp check's to ask about
            continue
        for log_id in owner_of[r["name"]]:  # a photo listed under two logs must fit each
            if log_id is None:
                days = sorted({f"{at:%d/%m}" for _, at in pickups.values()})
                why = f"the disposal's logs were picked up on {and_list(days)}"
            elif log_id in pickups:
                entry, at = pickups[log_id]
                days, why = [f"{at:%d/%m}"], f"{log_name(entry)} was picked up on {at:%d/%m}"
            else:
                continue
            checked += 1
            if m["date"] not in days:
                wrong.append((r["name"], m, why))
    if not checked:
        return None
    label = "Collection photos taken on the pickup day"
    if not wrong:
        return check("collection", "collection_photo_date", label, "pass",
                     f"{checked} collection photo{'s' if checked > 1 else ''} stamped on the pickup day")
    detail = "; ".join(f"{name} was stamped {m['date']}, but {why}" for name, m, why in wrong)
    return check("collection", "collection_photo_date", label, "review",
                 f"{detail}. Every collection photo must be taken on its pickup day: check the photo, or the pickup date "
                 "entered.", min(m["certainty"] for _, m, _ in wrong), images=list(dict.fromkeys(name for name, _, _ in wrong)))


def too_late(request: dict, images: dict[str, list[dict]], around=None) -> dict | None:
    """No unloading photo stamped more than LATE_AFTER_END after the end time entered. None without an end time or
    a readable unloading stamp. `around`, the trip's date, gives the stamps their year."""
    end = standards.ist_time((request.get("claims") or {}).get("endTime"))
    stamped = [(r, m) for r, m in photo_moments(images.get("unloading", []), around) if m]
    if not end or not stamped:
        return None
    limit = end + LATE_AFTER_END
    # As the stamps are read: in their year by the trip's date, or without one in proof_check.YEAR.
    by = datetime(limit.year if around else proof_check.YEAR, limit.month, limit.day, limit.hour, limit.minute)
    late = [(r["name"], m) for r, m in stamped if m["at"] > by]
    label = "Unloading photos by the end time"
    if not late:
        return check("unloading", "unloading_too_late", label, "pass",
                     f"Every unloading photo by {limit:%H:%M} on {limit:%d/%m} (the end time {end:%H:%M} + 1 h 30 min)")
    detail = "; ".join(f"{name} was stamped {m['time']} on {m['date']}" for name, m in late)
    return check("unloading", "unloading_too_late", label, "review",
                 f"{detail}: more than 1 h 30 min after the end time entered, {end:%H:%M} on {end:%d/%m}. Check the end "
                 "time entered, or when the photo was taken.", min(m["certainty"] for _, m in late),
                 images=[name for name, _ in late])


def after_collections(request: dict) -> dict | None:
    """The disposal's arrival entered after every log's pickup; by date only for a manual entry (the disposal's or
    the log's). None without an arrival time or a pickup."""
    arrival = standards.ist_time((request.get("claims") or {}).get("arrivalTime"))
    entries = [(e, at) for e in logs(request) if (at := pickup(e))]
    if not arrival or not entries:
        return None
    manual_disposal = (request.get("subject") or {}).get("createdVia") == MANUAL
    late = []
    for e, at in entries:
        manual = manual_disposal or e.get("createdVia") == MANUAL
        if (at.date() > arrival.date()) if manual else at > arrival:
            late.append(f"{log_name(e)} was picked up on {at:%d/%m}" if manual else
                        f"{log_name(e)} was picked up at {at:%H:%M} on {at:%d/%m}")
    label = "Collections before the disposal"
    if not late:
        return check("collection", "disposal_after_collections", label, "pass",
                     f"All {len(entries)} pickups before the arrival entered, {arrival:%H:%M} on {arrival:%d/%m}")
    return check("collection", "disposal_after_collections", label, "review",
                 f"{'; '.join(late)}: after the disposal's arrival entered, {arrival:%H:%M} on {arrival:%d/%m}. The waste "
                 "is collected before it is disposed of: check the times entered.")


def point_of(entry: dict) -> tuple[float, float] | None:
    """A collection log's collection point, as (latitude, longitude), if gw_sales knows it (in India or not)."""
    where = (entry.get("collectionPoint") or {}).get("location") or {}
    return (where["lat"], where["lng"]) if where.get("lat") is not None and where.get("lng") is not None else None


def known_point(entry: dict) -> tuple[float, float] | None:
    """The collection point, if it is known and in India: one outside (latitude and longitude swapped) isn't used."""
    point = point_of(entry)
    return point if point and proof_check.in_india(*point) else None


def photos_to_place(request: dict) -> set[str]:
    """The collection photos whose GPS position Jev should read: those of logs whose collection point is known."""
    known = {e["id"] for e in logs(request) if known_point(e)}
    return {key for key, ids in owners(request).items() if known & set(ids)}


def collection_points(request: dict, images: dict[str, list[dict]], radius: float) -> list[dict]:
    """For each log whose collection point is known, at least one of its photos stamped within `radius` of it; any
    clearly farther is a problem (`proof_check.near_check`, as unloading at the plant). Aimed at the log."""
    owner_of, made = owners(request), []
    for e in logs(request):
        point = point_of(e)
        photos = [r for r in images.get("loading", []) if point and e["id"] in owner_of.get(r["name"], [])]
        if not photos:  # no point known, or no photo of it to check (has_photos asks for one)
            continue
        name = (e.get("collectionPoint") or {}).get("name")
        where = f"the collection point of {log_name(e)}" if not name else f"{name}, the collection point of collection log {e['id']}"
        if known_point(e):
            c = proof_check.near_check("collection", "collection_point", "At the collection point", photos, point,
                                       radius, where, "collection")
        else:
            c = check("collection", "collection_point", "At the collection point", "review",
                      f"The location in gw_sales of {where}, {point[0]}, {point[1]}, isn't in India (latitude and "
                      "longitude may be swapped), so its collection photos weren't checked against it: fix it in "
                      "gw_sales and check the photos' GPS stamps.", images=[r["name"] for r in photos])
        c["target"] = {"type": "collection_log", "id": e["id"]}
        made.append(c)
    return made


def log_reused(request: dict, other_logs: list[tuple[int, dict]]) -> list[dict]:
    """A collection log is one pickup, never split over two disposals: one problem per other disposal that holds any
    of this one's log ids, naming every shared log. Aimed at the log when only one is shared. `other_logs` is
    (disposal id, log) as `database.other_logs` finds them."""
    label = "No collection log is on another disposal"
    made = []
    for i in sorted({i for i, _ in other_logs}):
        ids = {other.get("id") for j, other in other_logs if j == i}
        shared = [e for e in logs(request) if e["id"] in ids]
        if not shared:
            continue
        names = [log_name(e, True) for e in shared]
        if len(shared) == 1:
            who, verb = names[0], "is"
        else:
            who, verb = f"collection logs {and_list([n.removeprefix('collection log ') for n in names])}", "are"
        c = check("collection", "log_reused", label, "fail",
                  f"{who[0].upper()}{who[1:]} {verb} also on disposal {i}. One pickup can't be counted in two disposals.",
                  images=[k for k, owned in owners(request).items() if {e["id"] for e in shared} & set(owned)])
        if len(shared) == 1:
            c["target"] = {"type": "collection_log", "id": shared[0]["id"]}
        c["related"] = compare_disposals.related([i])
        made.append(c)
    return made


def pickup_key(entry: dict) -> tuple | None:
    """What makes two collection logs one pickup: client, collection point, IST day and every material with its
    quantity and unit. None if any is missing, so a log isn't matched on blanks."""
    client = ((entry.get("client") or {}).get("name") or "").strip().lower()
    point = ((entry.get("collectionPoint") or {}).get("name") or "").strip().lower()
    day = standards.ist_day(entry.get("collectionDate"))
    try:
        materials = frozenset((m["materialName"].strip().lower(), float(m["quantity"]), m["unit"].strip().lower())
                              for m in entry.get("materials") or [])
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    return (client, point, day, materials) if client and point and day and materials else None


def log_repeated(request: dict, other_logs: list[tuple[int, dict]]) -> list[dict]:
    """The same pickup entered twice under different ids (same client, collection point, IST day and materials with
    quantities): one check per pair, this disposal's logs pairwise and against `other_logs`, asking a person. A pair
    with a log that `log_reused` flagged for that disposal is its, not this one's."""
    entries = [e for e in logs(request) if pickup_key(e)]
    label = "No collection log repeats another pickup"
    shared = {i: {o["id"] for j, o in other_logs if j == i} & {e["id"] for e in logs(request)} for i, _ in other_logs}
    pairs = [(a, b, None) for n, a in enumerate(entries) for b in entries[n + 1:]]
    pairs += [(a, b, i) for a in entries for i, b in other_logs if pickup_key(b)]
    found: dict[int | None, list[tuple[dict, dict]]] = {}
    for a, b, i in pairs:
        if a["id"] == b["id"] or pickup_key(a) != pickup_key(b) or (i is not None and {a["id"], b["id"]} & shared[i]):
            continue
        found.setdefault(i, []).append((a, b))
    made = []
    for i, matched in found.items():
        where = "on this disposal" if i is None else f"on disposal {i}"
        said = []
        for a, b in matched:
            name = log_name(a)
            said.append(f"{name[0].upper()}{name[1:]} and collection log {b['id']} ({where}) are the same pickup: same "
                        f"client, collection point, day {pickup(a):%d/%m} and weights.")
        ours = {a["id"] for a, _ in matched}
        c = check("collection", "log_repeated", label, "review",
                  f"{' '.join(said)} Check {'it wasn' if len(matched) == 1 else 'they weren'}'t entered twice.",
                  images=[k for k, owned in owners(request).items() if ours & set(owned)])
        if len(matched) == 1:
            c["target"] = {"type": "collection_log", "id": matched[0][0]["id"]}
        if i is not None:
            c["related"] = compare_disposals.related([i])
        made.append(c)
    return made


def checks(request: dict, images: dict[str, list[dict]], radius: float = 500, around=None,
           other_logs: list[tuple[int, dict]] = ()) -> list[dict]:
    """Every check of what was entered, for one disposal's request and its checked images; `radius` is how far from
    its collection point a collection photo may be, `around` the trip's date, and `other_logs` the collection logs of
    other disposals (`database.other_logs`)."""
    return [*has_photos(request), *log_same_photo(request, images), *log_reused(request, other_logs),
            *log_repeated(request, other_logs),
            *(c for c in (photo_date(request, images, around), too_late(request, images, around),
                          after_collections(request)) if c),
            *collection_points(request, images, radius)]


def collected_vs_weighed(request: dict) -> dict | None:
    """What the collection logs weighed by hand scale against the net weight entered, for gw_sales to see and for
    choosing a tolerance later. Not a check: nothing is flagged. Bags are counted apart, as they have no weight."""
    entries = logs(request)
    if not entries:
        return None
    kg = bags = 0.0
    for e in entries:
        for material in e.get("materials") or []:
            quantity, unit = material.get("quantity"), (material.get("unit") or "").lower()
            if quantity is None:
                continue
            if unit in ("kg", "kgs"):
                kg += quantity
            elif "bag" in unit:
                bags += quantity
    claims = request.get("claims") or {}
    net = claims.get("quantity") if (claims.get("unit") or "kg").lower() in ("kg", "kgs") else None
    return {"logs": len(entries), "collectedKg": round(kg, 2), "collectedBags": bags, "netEnteredKg": net,
            "differenceKg": round(net - kg, 2) if net is not None and kg else None}
