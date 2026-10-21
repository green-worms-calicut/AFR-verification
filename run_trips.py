"""Check every trip folder under a folder with Jev, and compare what Jev read with hand-read values.

A trip folder holds collection*/loading* photos, unloading* photos and weighbill* images, as in the
inward log export. What was typed for the trip comes from trip.json in its folder:
    {"vehicle_number": "KL57AB8815", "quantity": 1630, "clock_slack": 10}
Without one, the hand-read vehicle number and final-slip net weight from truth.jsonl (in the folder
given or a parent) stand in for it, so the run checks Jev against the images themselves.

Usage:
    .venv/bin/python run_trips.py trips/inward-2026-10-19              # asks Jev, saves answers in eval/
    .venv/bin/python run_trips.py trips/inward-2026-10-19 --cached     # re-checks saved answers, no requests
    .venv/bin/python run_trips.py trips/inward-2026-10-19 --only 171773 171496 --model jev-preview

Saved answers are reused whenever the image, questions, typed values and model are the same, so a
second run only pays for what changed. Images already used in another trip, and a vehicle on two
trips at once, are flagged across the whole folder.
"""

import argparse
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from typesafe_sdk import TypeSafeClient

import check_proof
from check_proof import ROLES

HERE = Path(__file__).parent
SHORT = {"pass": "Accepted", "problem": "PROBLEM", "review": "check"}


def find_trips(root: Path) -> list[Path]:
    """Folders under `root` that hold trip images, in name order."""
    return sorted({p.parent for p in root.rglob("*") if p.is_file() and check_proof.role_of(p.name)})


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
            plate = t.get("plate")
            if plate:
                expected = "same" if plate == number else "cannot_tell" if plate in ("unreadable", "not_visible") else "different"
                add("plate matches", r["name"], expected, r["answers"]["plate_number"]["choice"])
    for r in trip["images"]["bill"]:
        t = truth["bills"].get(r["name"])
        if not t:
            continue
        kind = {"final": "both_weights", "first": "one_weight", "other": check_proof.NOT_SHOWN}[t["slip"]]
        add("slip kind", r["name"], kind, r["answers"]["slip_kind"]["choice"])
        if t["slip"] != "other":
            add("bill number", r["name"], "same" if t.get("vehicle_number") == number else "different",
                r["answers"]["bill_number"]["choice"])
    events = {e["what"]: e["time"] for e in trip["verdict"]["timeline"]}
    final = next((b for b in truth["bills"].values() if b["slip"] == "final"), {})
    add("tare time", "final slip", hhmm(final.get("tare_at")), events.get("Tare"))
    add("gross time", "final slip", hhmm(final.get("gross_at")), events.get("Gross"))
    order = next((c["detail"] for c in trip["verdict"]["checks"] if c["key"] == "weigh_order"), "")
    got_case = "tare_first" if order.startswith("Tare first") else "gross_first" if order.startswith("Gross first") else "unclear"
    add("weighing order", "trip", truth["case"], got_case)
    return items


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", type=Path, help="a folder of trip folders, such as trips/inward-2026-10-19")
    parser.add_argument("--only", nargs="+", default=[], help="trip ids (folder names) to check")
    parser.add_argument("--cached", action="store_true", help="use saved answers only; trips without them are skipped")
    parser.add_argument("--model", default="jev-latest", help="jev-latest (1.13), jev-preview (1.14), or a pinned ID")
    parser.add_argument("--workers", type=int, default=2, help="trips checked at once (each asks about its images at once)")
    parser.add_argument("--json", type=Path, help="also write every trip's full result to this file")
    args = parser.parse_args()

    folders = [f for f in find_trips(args.root) if not args.only or f.name in args.only]
    truth = find_truth(args.root)
    index = check_proof.ImageIndex.of_folders(args.root)
    cache_path = HERE / "eval" / f"answers-{args.model}.json"
    cache = json.loads(cache_path.read_text()) if cache_path.is_file() else {}
    lock = threading.Lock()
    print(f"{len(folders)} trips, {len(index.items)} images in the folder. {len(cache)} saved answers in "
          f"{cache_path.relative_to(HERE)}.", file=sys.stderr)

    client = None
    if not args.cached:
        if not (key := check_proof.api_key()):
            sys.exit(check_proof.NO_KEY)
        client = TypeSafeClient(api_key=key, headers=check_proof.HEADERS, timeout=check_proof.TIMEOUT)

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
        number = check_proof.normalize_vehicle_number(typed["vehicle_number"])
        quantity = check_proof.parse_quantity(typed.get("quantity"))
        limits = check_proof.parse_limits(typed)
        try:
            trip, _ = check_proof.analyze_trip(client, check_proof.read_trip_folder(folder), number, args.model,
                                               quantity, limits, index, trip_id, saved)
        except Exception as error:
            skipped[folder.name] = f"{type(error).__name__}: {error}"
            return
        results[folder.name] = {**trip, "vehicle_number": number, "quantity": quantity, "typed_from": source}
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

    for trip, c in check_proof.overlap_checks(results).items():
        check_proof.add_checks(results[trip], [c])

    print(f"\n{'trip':8} {'vehicle':11} {'verdict':9} first flags")
    for name in sorted(results):
        r = results[name]
        flags = "; ".join(f"{'✕' if c['status'] == 'fail' else '?'} {c['label']}: {c['detail']}" for c in r["verdict"]["flags"][:3])
        print(f"{name:8} {r['vehicle_number']:11} {SHORT[r['verdict']['status']]:9} {flags[:150]}")
    for name, why in sorted(skipped.items()):
        print(f"{name:8} {'':11} {'skipped':9} {why}")

    counts = {s: sum(r["verdict"]["status"] == s for r in results.values()) for s in SHORT}
    tokens = sum(i["input_tokens"] for r in results.values() for rs in r["images"].values() for i in rs if not i.get("saved"))
    print(f"\n{counts['pass']} accepted, {counts['problem']} with a problem, {counts['review']} to check, {len(skipped)} skipped. "
          f"{tokens:,} new input tokens ≈ ${tokens * check_proof.PRICE_PER_INPUT_TOKEN:.4f} (saved answers cost nothing)")

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
