"""Count images that only look alike (a re-saved or resized copy, not the same file) across all saved images, to
decide whether look-alikes should be looked for over all history instead of the same vehicle's trips within a day.

    .venv/bin/python -m tools.measure_look_alikes --trips trips/
    .venv/bin/python -m tools.measure_look_alikes --db postgresql://verifier@127.0.0.1:5433/verifier

--trips fingerprints the images of every trip folder under it (no Jev). Today's window there is the same date folder
(trips/inward-2026-10-23/...) and the same vehicle, from trip.json or the hand-read truth.jsonl; a trip with no
vehicle is outside every other trip's window. --db reads the fingerprints already saved in each disposal's latest
analysis (read-only, nothing is migrated or written); its window is the same vehicle with trip days a day apart.

Prints how many look-alike pairs fall inside and outside the window, and writes eval/look-alikes-YYYY-MM-DD.html
listing every pair outside it, for a person to mark which are real copies. No Jev, no S3. eval/ is never committed.
"""

import argparse
import html
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proof_check  # noqa: E402
from proof_check import FEATURELESS, LOOKS_ALIKE  # noqa: E402
from tools import check_trip_folders  # noqa: E402


def pairs(items: list[tuple[str, str, str, int]], window) -> list[tuple]:
    """Look-alike pairs (name a, name b, group a, group b) of (group, name, md5, dhash) items: in different groups,
    not the same file, at most LOOKS_ALIKE bits apart, neither plain, and outside the window(group a, group b).
    Copies of one file give one pair with another image, not one each."""
    plain = lambda h: not FEATURELESS < bin(h).count("1") < 64 - FEATURELESS  # noqa: E731
    found, seen = [], set()
    for i, (ga, na, ma, ha) in enumerate(items):
        if plain(ha):
            continue
        for gb, nb, mb, hb in items[i + 1:]:
            if ga == gb or ma == mb or plain(hb) or bin(ha ^ hb).count("1") > LOOKS_ALIKE or window(ga, gb):
                continue
            if (key := frozenset((ma, mb))) not in seen:
                seen.add(key)
                found.append((na, nb, ga, gb))
    return found


def trip_items(root: Path):
    """(items, window) of every trip folder under `root`."""
    truth = check_trip_folders.find_truth(root)
    vehicles = {}
    for folder in check_trip_folders.find_trips(root):
        typed, _ = check_trip_folders.typed_for(folder, truth.get(folder.name))
        vehicles[folder.relative_to(root).as_posix()] = (typed or {}).get("vehicle_number")

    def window(a, b):
        return a.split("/")[0] == b.split("/")[0] and vehicles.get(a) is not None and vehicles.get(a) == vehicles.get(b)

    return proof_check.ImageIndex.of_folders(root).items, window


def db_items(url: str):
    """(items, window) from each disposal's latest request's saved analysis. The group is the disposal id."""
    import psycopg
    with psycopg.connect(url, options="-c default_transaction_read_only=on") as conn:
        rows = conn.execute(
            """SELECT disposal_id, vehicle_number, trip_day, img->>'name', img->'prints'->>'md5', img->'prints'->>'dhash'
               FROM (SELECT DISTINCT ON (q.disposal_id) q.disposal_id, q.vehicle_number, q.trip_day, r.analysis
                     FROM verification_request q LEFT JOIN verification_result r USING (request_id)
                     ORDER BY q.disposal_id, q.received_at DESC) latest,
                    jsonb_each(analysis->'images') AS k(kind, list), jsonb_array_elements(k.list) AS img
               WHERE analysis IS NOT NULL AND img->'prints' IS NOT NULL""").fetchall()
    trips = {str(d): (v, day) for d, v, day, *_ in rows}
    items = [(str(d), name, md5, int(dhash, 16)) for d, _, _, name, md5, dhash in rows]

    def window(a, b):
        (va, da), (vb, db) = trips[a], trips[b]
        return va is not None and va == vb and da is not None and db is not None and abs((da - db).days) <= 1

    return items, window


def write_report(path: Path, sections: list[tuple[str, list[tuple], object]]) -> None:
    """One table per source, each pair with both images: <img> for a local file, the s3Key for the database."""
    out = ["<!doctype html><meta charset=utf-8><title>Look-alike pairs</title><body style='font-family:sans-serif'>"]
    for title, found, show in sections:
        out.append(f"<h2>{html.escape(title)}: {len(found)} pairs outside the window</h2><table border=1 cellpadding=4>")
        for na, nb, ga, gb in found:
            out.append("<tr>" + "".join(f"<td>{show(g, n)}</td>" for g, n in ((ga, na), (gb, nb))) + "</tr>")
        out.append("</table>")
    path.parent.mkdir(exist_ok=True)
    path.write_text("\n".join(out))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", help="Postgres URL of the verifier database (read-only)")
    ap.add_argument("--trips", type=Path, help="folder of trip folders, e.g. trips/")
    args = ap.parse_args()
    if not (args.db or args.trips):
        ap.error("give --db, --trips, or both")
    sections = []
    if args.trips:
        root = args.trips
        items, window = trip_items(root)
        shown = lambda g, n: f"<img src='{html.escape(os.path.relpath(root / g / n, ROOT / 'eval'), quote=True)}' width=300><br>{html.escape(g)}/{html.escape(n)}"  # noqa: E731
        sections.append(("trips", items, window, shown))
    if args.db:
        items, window = db_items(args.db)
        sections.append(("database", items, window, lambda g, n: f"disposal {html.escape(g)}<br>s3Key {html.escape(n)}"))
    report = []
    for title, items, window, shown in sections:
        outside = pairs(items, window)
        total = len(pairs(items, lambda a, b: False))
        print(f"{title}: {len(items)} images, {total - len(outside)} look-alike pairs inside the window, {len(outside)} outside")
        report.append((title, outside, shown))
    path = ROOT / "eval" / f"look-alikes-{date.today().isoformat()}.html"
    write_report(path, report)
    print(f"report: {path}")


if __name__ == "__main__":
    main()
