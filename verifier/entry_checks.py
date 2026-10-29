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
from verifier import standards

LATE_AFTER_END = timedelta(hours=1, minutes=30)  # how long after the end time entered an unloading photo may be
MANUAL = "MANUAL_TRIP_ENTRY"  # entered after the fact: its times can be hours or days off, so only its dates count


def logs(request: dict) -> list[dict]:
    return (request.get("reference") or {}).get("collectionLogs") or []


def owners(request: dict) -> dict[str, int | None]:
    """Each collection photo's s3Key and its collection log's id, or None for a photo of the disposal itself."""
    found = {}
    for item in request.get("evidence") or []:
        if item.get("category") == "collection" and item.get("s3Key"):
            owner = item.get("owner") or {}
            found[item["s3Key"]] = owner.get("id") if owner.get("type") == "collection_log" else None
    return found


def pickup(entry: dict) -> datetime | None:
    """A collection log's pickup (its collectionDate) in IST."""
    return standards.ist_time(entry.get("collectionDate"))


def log_name(entry: dict) -> str:
    """'collection log 45001 (ABC Hotel, Main Kitchen)'."""
    where = [x for x in ((entry.get("client") or {}).get("name"), (entry.get("collectionPoint") or {}).get("name")) if x]
    return f"collection log {entry['id']}" + (f" ({', '.join(where)})" if where else "")


def has_photos(request: dict) -> list[dict]:
    """Every collection log has a photo of its own: one check per log without, aimed at that log."""
    entries = logs(request)
    if not entries:
        return []
    taken = {i for i in owners(request).values() if i is not None}
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


def photo_date(request: dict, images: dict[str, list[dict]]) -> dict | None:
    """Every collection photo stamped on its log's pickup day; a photo of the disposal itself, on the pickup day of
    one of its logs. None when no photo has a readable stamp and a pickup to compare with."""
    pickups = {e["id"]: (e, at) for e in logs(request) if (at := pickup(e))}
    if not pickups:
        return None
    owner_of = owners(request)
    wrong, checked = [], 0
    for r, m in photo_moments(images.get("loading", [])):
        if m is None or r["name"] not in owner_of:  # an unreadable stamp is the GPS stamp check's to ask about
            continue
        log_id = owner_of[r["name"]]
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
                 "entered.", min(m["certainty"] for _, m, _ in wrong), images=[name for name, _, _ in wrong])


def too_late(request: dict, images: dict[str, list[dict]]) -> dict | None:
    """No unloading photo stamped more than LATE_AFTER_END after the end time entered. None without an end time or
    a readable unloading stamp."""
    end = standards.ist_time((request.get("claims") or {}).get("endTime"))
    stamped = [(r, m) for r, m in photo_moments(images.get("unloading", [])) if m]
    if not end or not stamped:
        return None
    limit = end + LATE_AFTER_END
    by = datetime(proof_check.YEAR, limit.month, limit.day, limit.hour, limit.minute)  # as stamps are read: no year
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
    """A collection log's collection point, as (latitude, longitude), if gw_sales knows it."""
    where = (entry.get("collectionPoint") or {}).get("location") or {}
    return (where["lat"], where["lng"]) if where.get("lat") is not None and where.get("lng") is not None else None


def photos_to_place(request: dict) -> set[str]:
    """The collection photos whose GPS position Jev should read: those of logs whose collection point is known."""
    known = {e["id"] for e in logs(request) if point_of(e)}
    return {key for key, log_id in owners(request).items() if log_id in known}


def collection_points(request: dict, images: dict[str, list[dict]], radius: float) -> list[dict]:
    """For each log whose collection point is known, at least one of its photos stamped within `radius` of it; any
    clearly farther is a problem (`proof_check.near_check`, as unloading at the plant). Aimed at the log."""
    owner_of, made = owners(request), []
    for e in logs(request):
        point = point_of(e)
        photos = [r for r in images.get("loading", []) if point and owner_of.get(r["name"]) == e["id"]]
        if not photos:  # no point known, or no photo of it to check (has_photos asks for one)
            continue
        name = (e.get("collectionPoint") or {}).get("name")
        c = proof_check.near_check("collection", "collection_point", "At the collection point", photos, point, radius,
                                   f"the collection point of {log_name(e)}" if not name else
                                   f"{name}, the collection point of collection log {e['id']}", "collection")
        c["target"] = {"type": "collection_log", "id": e["id"]}
        made.append(c)
    return made


def checks(request: dict, images: dict[str, list[dict]], radius: float = 500) -> list[dict]:
    """Every check of what was entered, for one disposal's request and its checked images; `radius` is how far from
    its collection point a collection photo may be."""
    return [*has_photos(request),
            *(c for c in (photo_date(request, images), too_late(request, images), after_collections(request)) if c),
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
