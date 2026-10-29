"""A trip's verdict and each part's, from its checks, and its timeline for the page.

Nothing is rejected here: a check that finds a problem flags its part, an unsure one asks a person.
"""

from proof_check.trip_rules import Limits, PARTS, photo_moments, trip_checks

# A check's status is pass, fail (a problem found) or review (unsure). Nothing is rejected without a person.
TITLES = {"pass": "Accepted", "problem": "Flagged: problem found", "review": "Flagged: please check"}


def timeline(images: dict[str, list[dict]], w: dict | None, checks: list[dict]) -> list[dict]:
    """The trip's events in time order, for the page: tare, loading, gross and unloading, marking those in a failed check."""
    failed = {c["key"] for c in checks if c["status"] == "fail"}
    events = []
    if w:
        events += [{"at": w["tare"], "what": "Tare", "bad": bool({"weigh_order", "weigh_gap", "weigh_day"} & failed)},
                   {"at": w["gross"], "what": "Gross", "bad": bool({"weigh_order", "weigh_gap", "weigh_day"} & failed)}]
    for role, word in (("loading", "Loading photos"), ("unloading", "Unloading photos")):
        times = [m for _, m in photo_moments(images.get(role, [])) if m]
        if times:
            first = min(times, key=lambda m: m["at"])
            events.append({"at": first, "what": word, "bad": f"{role}_timing" in failed or f"{role}_day" in failed})
    events.sort(key=lambda e: e["at"]["at"])
    return [{"time": e["at"]["time"], "date": e["at"]["date"], "what": e["what"], "bad": e["bad"]} for e in events]


def decide(checks: list[dict]) -> dict:
    """The verdict from a list of checks: any failed check flags a problem, any unsure one asks a person.

    Nothing is rejected here. `flags` lists the checks that didn't pass, problems first.
    """
    statuses = {c["status"] for c in checks}
    status = "problem" if "fail" in statuses else "review" if "review" in statuses else "pass"
    flags = sorted((c for c in checks if c["status"] != "pass"), key=lambda c: c["status"] != "fail")
    return {"status": status, "title": TITLES[status], "checks": checks, "flags": flags}


def decide_parts(checks: list[dict]) -> dict:
    """The trip's verdict from all its checks, so the worst of its parts, and each part's from its own (`PARTS`).

    Only parts with checks are kept: a trip that isn't a Verra project has no Verra part.
    """
    parts = {p: decide([c for c in checks if c["part"] == p]) for p in PARTS if any(c["part"] == p for c in checks)}
    return {**decide(checks), "parts": parts}


def verdict(images: dict[str, list[dict]], vehicle_number: str, quantity: str | None = None,
            limits: Limits = Limits(), extra: list[dict] = (), plant: tuple[float, float] | None = None,
            verra: bool = False) -> dict:
    """The trip's verdict and each part's, with its checks, flags and timeline. `extra` are checks across trips."""
    checks, w = trip_checks(images, vehicle_number, quantity, limits, plant, verra)
    checks += list(extra)
    # Between the two weighings, for finding a vehicle on two trips at once: only when their order and day are clear.
    # A stored tare (`weigh_day` failed) would stretch it across a day and overlap the vehicle's other trips.
    clear = all(any(c["key"] == key and c["status"] == "pass" for c in checks) for key in ("weigh_order", "weigh_day"))
    window = sorted([w["tare"]["at"], w["gross"]["at"]]) if w and clear else None
    return {**decide_parts(checks), "timeline": timeline(images, w, checks),
            "window": [t.isoformat() for t in window] if window else None}


def add_checks(trip: dict, extra: list[dict]) -> None:
    """Add checks across trips to a finished trip's verdict, and decide it and its parts again."""
    v = trip["verdict"]
    trip["verdict"] = {**v, **decide_parts(v["checks"] + extra)}
