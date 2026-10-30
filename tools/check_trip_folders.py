"""Check every trip folder under a folder with Jev, and compare what Jev read with hand-read values.

A trip folder holds collection*/loading* photos, unloading* photos and weighbill* images, as in the
inward log export. What was typed for the trip comes from trip.json in its folder:
    {"vehicle_number": "KL57AB8815", "quantity": 1630, "clock_slack": 10, "plant": "11.4404, 75.9420", "verra": true,
     "weighbridge": "G W Electronic Weigh Bridge"}
Without one, the hand-read vehicle number and final-slip net weight from truth.jsonl (in the folder
given or a parent) stand in for it, so the run checks Jev against the images themselves. The plant's
location comes from --plant for every trip, unless a trip.json gives its own. "verra": true marks a Verra
project trip, whose final slip must have a material entered and carry the weighbridge's stamp. "weighbridge" is the
weighbridge entered, whose name the final slip must show.

Each trip is reported by part (real, docs, verra: see proof_check.PARTS) and as a whole.

Usage:
    .venv/bin/python -m tools.check_trip_folders trips/inward-2026-10-19              # asks Jev, saves answers in eval/
    .venv/bin/python -m tools.check_trip_folders trips/inward-2026-10-19 --plant "11.4404, 75.9420"   # and checks unloading at the plant
    .venv/bin/python -m tools.check_trip_folders trips/inward-2026-10-19 --cached     # re-checks saved answers, no requests
    .venv/bin/python -m tools.check_trip_folders trips/inward-2026-10-19 --only 171773 171496 --model jev-preview

Saved answers are reused whenever the image, questions, typed values and model are the same, so a
second run only pays for what changed. Images already used in another trip, and a vehicle on two
trips at once, are flagged across the whole folder.
"""

import argparse
import json
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from typesafe_sdk import TypeSafeClient

import proof_check

ROOT = Path(__file__).resolve().parents[1]  # the project folder: eval/ is kept there
SHORT = {"pass": "Accepted", "problem": "PROBLEM", "review": "check"}


def find_trips(root: Path) -> list[Path]:
    """Folders under `root` that hold trip images, in name order."""
    return sorted({p.parent for p in root.rglob("*") if p.is_file() and proof_check.role_of(p.name)})


def find_truth(root: Path) -> dict[str, dict]:
    """Hand-read values by trip id, from truth.jsonl in `root` or a parent."""
    for folder in (root, *root.resolve().parents):
        if (path := folder / "truth.jsonl").is_file():
            return {r["trip"]: r for r in map(json.loads, path.read_text().splitlines()) if r}
    return {}


def typed_for(folder: Path, truth: dict | None) -> tuple[dict | None, str]:
    """What was typed for a trip, and where it came from: trip.json, or the hand-read values."""
    if (path := folder / "trip.json").is_file():
        return json.loads(path.read_text()), "trip.json"
    if truth and truth.get("vehicle_number"):
        return {"vehicle_number": truth["vehicle_number"], "quantity": truth.get("net_kg")}, "hand-read"
    return None, "nothing typed"


def hhmm(text: str | None) -> str | None:
    """'19/10/2026 07:39' -> '07:39'."""
    return text.split()[-1] if text else None


def cut(degrees: float, places: int) -> str:
    """11.440404, 4 -> '11.4404': cut off at `places` decimals, as Jev reads them."""
    whole, _, decimals = f"{degrees:.6f}".partition(".")
    return f"{whole}.{decimals[:places]}"


def compare(trip: dict, truth: dict, typed: dict) -> list[dict]:
    """What Jev read against the hand-read values, one item per value both have."""
    items = []

    def add(field, image, expected, got):
        if expected is not None:
            items.append({"field": field, "image": image, "expected": expected, "got": got, "right": expected == got})

    number = typed["vehicle_number"]
    for role in ("loading", "unloading"):
        for r in trip["images"][role]:
            t = truth["photos"].get(r["name"], {})
            reading = next(x for x in r["readings"] if x["key"] == "photo_time")
            add("photo time", r["name"], hhmm(t.get("stamp_at")), reading["value"])
            place = next((x for x in r["readings"] if x["key"] == "position"), None)
            if place and t.get("lat") is not None:  # to the decimals Jev read: four when picked, otherwise two
                places = len((place["value"] or "0.00").split(", ")[0].partition(".")[2])
                add("photo position", r["name"], f"{cut(t['lat'], places)}, {cut(t['long'], places)}", place["value"])
            plate = t.get("plate")
            if plate:
                expected = "same" if plate == number else "cannot_tell" if plate in ("unreadable", "not_visible") else "different"
                add("plate matches", r["name"], expected, r["answers"]["plate_number"]["choice"])
    for r in trip["images"]["bill"]:
        t = truth["bills"].get(r["name"])
        if not t:
            continue
        kind = {"final": "both_weights", "first": "one_weight", "other": proof_check.NOT_SHOWN}[t["slip"]]
        add("slip kind", r["name"], kind, r["answers"]["slip_kind"]["choice"])
        if t["slip"] != "other":
            add("bill number", r["name"], "same" if t.get("vehicle_number") == number else "different",
                r["answers"]["bill_number"]["choice"])
    events = {e["what"]: e["time"] for e in trip["verdict"]["timeline"]}
    final = next((b for b in truth["bills"].values() if b["slip"] == "final"), {})
    add("tare time", "final slip", hhmm(final.get("tare_at")), events.get("Tare"))
    add("gross time", "final slip", hhmm(final.get("gross_at")), events.get("Gross"))
    got_case = next((c.get("order", "unclear") for c in trip["verdict"]["checks"] if c["key"] == "weigh_order"), "unclear")
    add("weighing order", "trip", truth["case"], got_case)
    return items


def trip_date(folder: Path) -> date:
    """The trip's date, for the stamps' year: the date in its folder's path (trips/inward-2026-10-19/...), or today."""
    found = re.search(r"(\d{4})-(\d{2})-(\d{2})", folder.as_posix())
    return date(*map(int, found.groups())) if found else date.today()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", type=Path, help="a folder of trip folders, such as trips/inward-2026-10-19")
    parser.add_argument("--only", nargs="+", default=[], help="trip ids (folder names) to check")
    parser.add_argument("--plant", help='the plant\'s latitude, longitude, such as "11.4404, 75.9420", for trips '
                                        "whose trip.json doesn't give one: unloading photos must be stamped near it")
    parser.add_argument("--cached", action="store_true", help="use saved answers only; trips without them are skipped")
    parser.add_argument("--model", default="jev-latest", help="jev-latest (1.13), jev-preview (1.14), or a pinned ID")
    parser.add_argument("--workers", type=int, default=2, help="trips checked at once (each asks about its images at once)")
    parser.add_argument("--json", type=Path, help="also write every trip's full result to this file")
    args = parser.parse_args()

    try:
        plant = proof_check.parse_plant(args.plant)
    except ValueError as error:
        parser.error(str(error))
    folders = [f for f in find_trips(args.root) if not args.only or f.name in args.only]
    truth = find_truth(args.root)
    index = proof_check.ImageIndex.of_folders(args.root)
    cache_path = ROOT / "eval" / f"answers-{args.model}.json"
    cache = json.loads(cache_path.read_text()) if cache_path.is_file() else {}
    lock = threading.Lock()
    print(f"{len(folders)} trips, {len(index.items)} images in the folder. {len(cache)} saved answers in "
          f"{cache_path.relative_to(ROOT)}.", file=sys.stderr)

    client = None
    if not args.cached:
        if not (key := proof_check.api_key()):
            sys.exit(proof_check.NO_KEY)
        client = TypeSafeClient(api_key=key, headers=proof_check.HEADERS, timeout=proof_check.TIMEOUT)

    class Saved(dict):
        """Saved answers; in --cached mode a missing one stops that trip instead of asking Jev."""

        def __contains__(self, key):
            found = dict.__contains__(self, key)
            if not found and args.cached:
                raise LookupError("no saved answers for an image; run without --cached first")
            return found

        def __setitem__(self, key, value):
            with lock:
                dict.__setitem__(self, key, value)

    saved = Saved(cache)
    results, skipped = {}, {}

    def run(folder: Path):
        trip_id = folder.relative_to(args.root).as_posix()
        typed, source = typed_for(folder, truth.get(folder.name))
        if not typed:
            skipped[folder.name] = "no trip.json and no hand-read vehicle number"
            return
        number = proof_check.normalize_vehicle_number(typed["vehicle_number"])
        quantity = proof_check.parse_quantity(typed.get("quantity"))
        try:
            limits = proof_check.parse_limits(typed)
            trip_plant = proof_check.parse_plant(typed["plant"]) if typed.get("plant") else plant
            verra = proof_check.parse_switch(typed.get("verra"), "verra")
            trip, _ = proof_check.analyze_trip(client, proof_check.read_trip_folder(folder), number, args.model,
                                               quantity, limits, index, trip_id, saved, trip_plant, verra,
                                               weighbridge=typed.get("weighbridge") or None, around=trip_date(folder))
        except Exception as error:
            skipped[folder.name] = f"{type(error).__name__}: {error}"
            return
        results[folder.name] = {**trip, "vehicle_number": number, "quantity": quantity, "plant": trip_plant, "verra": verra,
                                "typed_from": source, "clock_slack": limits.clock_slack}
        with lock:
            cache_path.parent.mkdir(exist_ok=True)
            cache_path.write_text(json.dumps(dict(saved)))
        v = trip["verdict"]
        print(f"  {folder.name}: {v['title']}", file=sys.stderr)

    try:
        with ThreadPoolExecutor(max(1, args.workers)) as pool:
            list(pool.map(run, folders))
    finally:
        if client:
            client.close()

    # Each trip may have its own limits (trip.json): the largest clock slack, so no pair is flagged for a few minutes
    # one of its trips allows.
    slack = max((r["clock_slack"] for r in results.values()), default=proof_check.Limits().clock_slack)
    for trip, c in proof_check.overlap_checks(results, slack).items():
        proof_check.add_checks(results[trip], [c])

    parts = " ".join(f"{p:9}" for p in proof_check.PARTS)
    print(f"\n{'trip':8} {'vehicle':11} {'verdict':9} {parts} first flags")
    for name in sorted(results):
        r = results[name]
        flags = "; ".join(f"{'✕' if c['status'] == 'fail' else '?'} {c['label']}: {c['detail']}" for c in r["verdict"]["flags"][:3])
        by_part = " ".join(f"{SHORT[r['verdict']['parts'][p]['status']] if p in r['verdict']['parts'] else '-':9}"
                           for p in proof_check.PARTS)
        print(f"{name:8} {r['vehicle_number']:11} {SHORT[r['verdict']['status']]:9} {by_part} {flags[:150]}")
    for name, why in sorted(skipped.items()):
        print(f"{name:8} {'':11} {'skipped':9} {why}")

    for part, title in proof_check.PARTS.items():
        decided = [r["verdict"]["parts"][part]["status"] for r in results.values() if part in r["verdict"]["parts"]]
        if decided:
            print(f"{title}: {decided.count('pass')} accepted, {decided.count('problem')} with a problem, "
                  f"{decided.count('review')} to check")
    counts = {s: sum(r["verdict"]["status"] == s for r in results.values()) for s in SHORT}
    tokens = sum(i["input_tokens"] for r in results.values() for rs in r["images"].values() for i in rs if not i.get("saved"))
    unanswered = sum(len(r.get("unanswered", [])) for r in results.values())
    not_read = f" {unanswered} images Jev couldn't answer for." if unanswered else ""
    print(f"\n{counts['pass']} accepted, {counts['problem']} with a problem, {counts['review']} to check, {len(skipped)} skipped.{not_read} "
          f"{tokens:,} new input tokens ≈ ${tokens * proof_check.PRICE_PER_INPUT_TOKEN:.4f} (saved answers cost nothing)")

    items = [dict(i, trip=name) for name, r in results.items() if name in truth for i in compare(r, truth[name], r)]
    if items:
        print("\nJev's readings against the hand-read values")
        fields = list(dict.fromkeys(i["field"] for i in items))
        for f in fields:
            g = [i for i in items if i["field"] == f]
            right = sum(i["right"] for i in g)
            print(f"  {f:15} {right:>4} of {len(g):<4} right ({100 * right / len(g):.0f}%)")
        wrong = [i for i in items if not i["right"]]
        print(f"\nWrong readings ({len(wrong)})")
        for i in wrong[:60]:
            print(f"  {i['trip']} {i['image']:16} {i['field']:15} read {i['got']!s:12} hand-read {i['expected']}")
        if len(wrong) > 60:
            print(f"  … and {len(wrong) - 60} more (use --json for all)")

    if args.json:
        for r in results.values():
            for rs in r["images"].values():
                for i in rs:
                    i.pop("parts", None)
        args.json.write_text(json.dumps({"trips": results, "skipped": skipped, "compared": items}, default=str, indent=1))
        print(f"\nWrote {args.json}")


if __name__ == "__main__":
    main()
