"""Measure how accurate Jev is on your own labeled images, and choose confidence thresholds from it.

1. Copy labels.example.csv to labels.csv. Add one row per image check: the file (relative to the
   CSV), its slot (photo or bill), the vehicle number typed in, for a bill the quantity typed in
   (kg), and the correct answers. Leave a cell blank to skip it. Add rows with a wrong vehicle
   number or quantity too (labeled `different`), to measure how well Jev catches a mismatch: one
   character or digit off is the hardest case.
2. Ask Jev about every row. This costs tokens, and saves the answers to eval/<model>.jsonl:
       .venv/bin/python evaluate.py labels.csv
3. Re-score the saved answers for free, for example after editing labels:
       .venv/bin/python evaluate.py labels.csv --cached

Label columns:
    shows_vehicle, gps_stamp, screen_photo, weigh_bill   yes or no
    slip_kind                              one_weight, both_weights or not_shown
    gross_vs_tare                          gross_heavier, tare_heavier or not_shown
    plate                                  readable, partly_readable or not_visible
    plate_number                           same, different or cannot_tell
    bill_number                            same, different or not_shown
    net_weight                             same, different or not_shown (needs a quantity)
    first_weight                           tare, gross or not_shown (which weight was taken at Time In)
    photo_time                             04:09, or not_shown
    gross_time, tare_time, in_time, out_time   07:39, or not_shown
    photo_date                             15/10/2026, or not_shown
    gross_date, tare_date, in_date, out_date   19/10/2026, or not_shown

The report shows accuracy per field, accuracy at each confidence level, and for each candidate
threshold how many answers would be accepted automatically and how many of those would be wrong.
For a yes/no answer, the confidence is how far it leans: the probability of yes for a yes, of no
for a no. So a threshold of 0.8 here matches check_proof.YES and NO at 0.8 and 0.2.
"""

import argparse
import csv
import json
import sys
from pathlib import Path

from typesafe_sdk import Noul, TypeSafeClient

import check_proof

FIELDS = {  # label column -> group
    "shows_vehicle": "image", "gps_stamp": "image", "screen_photo": "image", "weigh_bill": "image",
    "slip_kind": "slips", "gross_vs_tare": "slips",
    "plate": "plate",
    "plate_number": "vehicle number", "bill_number": "vehicle number",
    "net_weight": "net weight",
    "first_weight": "weighing order",
    "photo_time": "times", "gross_time": "times", "tare_time": "times", "in_time": "times", "out_time": "times",
    "photo_date": "dates", "gross_date": "dates", "tare_date": "dates", "in_date": "dates", "out_date": "dates",
}
BUCKETS = [(0.0, 0.5), (0.5, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 0.95), (0.95, 1.01)]
THRESHOLDS = [0.5, 0.6, 0.7, 0.8, 0.9, 0.95]
SMALL_SAMPLE = 30  # below this many answers, accuracy figures are rough
READING_FIELDS = {key for readings in check_proof.REPORT.values() for key, *_ in readings}


def row_id(row: dict) -> str:
    quantity = f" | {row['quantity']} kg" if row.get("quantity") else ""
    return f"{row['file']} | {row['slot']} | {row['vehicle_number']}{quantity}"


def normalize(field: str, value: str) -> str:
    """Write labels and answers the same way: 3:09 -> 03:09, 15/10/2026 -> 15/10, Yes -> yes."""
    value = value.strip().lower()
    if value in ("", check_proof.NOT_SHOWN) or field not in READING_FIELDS:
        return value
    sep = ":" if "time" in field else "/"
    return sep.join(part.zfill(2) for part in value.replace(".", sep).split(sep)[:2])


def allowed(slot: str, field: str) -> set[str] | None:
    """The values a label may take for this slot and field, or None if the field isn't asked about this slot."""
    question = check_proof.QUESTIONS[slot].get(field)
    if isinstance(question, Noul):
        return {"yes", "no"}
    if question is not None:
        return set(question.criteria)
    return set() if any(field == key for key, *_ in check_proof.REPORT[slot]) else None


def load_labels(path: Path) -> list[dict]:
    with path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    for n, row in enumerate(rows, 2):
        where = f"{path}, line {n}"
        row["slot"] = row.get("slot", "").strip().lower()
        if row["slot"] not in check_proof.SLOTS:
            sys.exit(f"{where}: slot must be photo or bill, not {row.get('slot')!r}.")
        number = check_proof.normalize_vehicle_number(row.get("vehicle_number", ""))
        if not number:
            sys.exit(f"{where}: vehicle_number {row.get('vehicle_number')!r} isn't 4 to 12 letters and digits.")
        row["vehicle_number"] = number
        try:
            row["quantity"] = check_proof.parse_quantity(row.get("quantity"))
        except ValueError:
            sys.exit(f"{where}: quantity {row.get('quantity')!r} isn't a number of kilograms.")
        if (row.get("net_weight") or "").strip() and not row["quantity"]:
            sys.exit(f"{where}: net_weight needs the quantity typed in. Fill in quantity, or leave net_weight blank.")
        row["path"] = (path.parent / row["file"]).resolve()
        for field in FIELDS:
            value = normalize(field, row.get(field) or "")
            if not value:
                continue
            ok = allowed(row["slot"], field)
            if ok is None:
                sys.exit(f"{where}: {field} isn't checked on a {row['slot']}. Leave it blank.")
            if ok and value not in ok:
                sys.exit(f"{where}: {field} must be one of {', '.join(sorted(ok))}, not {value!r}.")
    return rows


def run(client: TypeSafeClient, rows: list[dict], model: str) -> dict[str, dict]:
    results = {}
    for i, row in enumerate(rows, 1):
        print(f"[{i}/{len(rows)}] {row_id(row)}", file=sys.stderr)
        try:
            img = check_proof.load(row["path"])
            results[row_id(row)], _ = check_proof.analyze(client, row["slot"], img, row["vehicle_number"], model,
                                                          row["quantity"])
        except Exception as error:  # keep going; the report lists what failed
            print(f"    failed: {type(error).__name__}: {error}", file=sys.stderr)
    return results


def score(rows: list[dict], results: dict[str, dict]) -> list[dict]:
    """One item per labeled cell: what was expected, what Jev answered, and how confident it was."""
    items = []
    for row in rows:
        result = results.get(row_id(row))
        if result is None:
            continue
        answers = result["answers"]
        readings = {r["key"]: r for r in result["readings"]}
        for field, group in FIELDS.items():
            expected = normalize(field, row.get(field) or "")
            if not expected:
                continue
            if field in readings:
                r = readings[field]
                got, confidence = (check_proof.NOT_SHOWN if r["missing"] else r["value"]), r["min_confidence"]
                detail = [(p["id"], p["choice"], round(p["confidence"], 2)) for p in r["parts"]]
            elif answers[field]["type"] == "noul":
                yes = answers[field]["yes"]
                got, confidence, detail = ("yes" if yes >= 0.5 else "no"), max(yes, 1 - yes), f"yes {yes:.2f}"
            else:
                a = answers[field]
                got, confidence, detail = a["choice"], a["confidence"], a["top"][:3]
            items.append({"row": row_id(row), "field": field, "group": group, "expected": expected,
                          "got": got, "correct": got == expected, "confidence": confidence, "detail": detail})
    return items


def table(headers: list[str], rows: list[list]) -> str:
    cells = [headers] + [[str(c) for c in row] for row in rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]
    lines = ["  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in cells]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(lines)


def pct(n: int, d: int) -> str:
    return f"{100 * n / d:.0f}%" if d else "-"


def report(items: list[dict], results: dict[str, dict], rows: list[dict], model: str) -> str:
    out = []
    cost = sum(r["cost_usd"] for r in results.values())
    tokens = sum(r["input_tokens"] for r in results.values())
    failed = [row_id(row) for row in rows if row_id(row) not in results]
    out.append(f"Model {model}: {len(results)} of {len(rows)} checks answered, {tokens:,} input tokens, ${cost:.4f}")
    if failed:
        out.append(f"No answers for: {', '.join(failed)}")

    out.append("\nAccuracy per field")
    out.append(table(["field", "answers", "correct", "accuracy"], [
        [f, len(g), sum(i["correct"] for i in g), pct(sum(i["correct"] for i in g), len(g))]
        for f in FIELDS if (g := [i for i in items if i["field"] == f])
    ]))

    for group in dict.fromkeys(FIELDS.values()):
        g = [i for i in items if i["group"] == group]
        if not g:
            continue
        out.append(f"\n{group}: accuracy at each confidence level")
        out.append(table(["confidence", "answers", "accuracy"], [
            [f"{lo:.2f}-{min(hi, 1):.2f}", len(b), pct(sum(i["correct"] for i in b), len(b))]
            for lo, hi in BUCKETS if (b := [i for i in g if lo <= i["confidence"] < hi])
        ]))
        out.append(f"\n{group}: what each threshold would do (accept at or above it, send the rest to a person)")
        rows_ = []
        for t in THRESHOLDS:
            accepted = [i for i in g if i["confidence"] >= t]
            wrong = sum(not i["correct"] for i in accepted)
            rows_.append([f"{t:.2f}", f"{len(accepted)} ({pct(len(accepted), len(g))})",
                          pct(len(accepted) - wrong, len(accepted)), wrong, len(g) - len(accepted)])
        out.append(table(["threshold", "accepted", "accepted correct", "accepted but wrong", "to a person"], rows_))
        if len(g) < SMALL_SAMPLE:
            out.append(f"Only {len(g)} answers: treat these figures as rough until there are at least {SMALL_SAMPLE}.")

    wrong = sorted((i for i in items if not i["correct"]), key=lambda i: -i["confidence"])
    out.append(f"\nMistakes ({len(wrong)}), most confident first")
    if wrong:
        out.append(table(["check", "field", "expected", "got", "confidence", "detail"], [
            [i["row"], i["field"], i["expected"], i["got"], f"{i['confidence']:.2f}", i["detail"]] for i in wrong
        ]))
    else:
        out.append("None.")
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("labels", type=Path, help="CSV of image checks and their correct answers")
    parser.add_argument("--model", default="jev-latest", help="jev-latest (1.13), jev-preview (1.14), or a pinned ID")
    parser.add_argument("--cached", action="store_true", help="re-score saved answers instead of asking Jev again")
    args = parser.parse_args()

    rows = load_labels(args.labels)
    cache = Path(__file__).parent / "eval" / f"{args.model}.jsonl"
    if args.cached:
        if not cache.is_file():
            sys.exit(f"No saved answers at {cache}. Run without --cached first.")
        results = {r["check"]: r["result"] for r in map(json.loads, cache.read_text().splitlines())}
    else:
        if not (key := check_proof.api_key()):
            sys.exit(check_proof.NO_KEY)
        with TypeSafeClient(api_key=key, headers=check_proof.HEADERS, timeout=check_proof.TIMEOUT) as client:
            results = run(client, rows, args.model)
        cache.parent.mkdir(exist_ok=True)
        cache.write_text("".join(json.dumps({"check": c, "result": r}) + "\n" for c, r in results.items()))
        print(f"Saved answers to {cache}", file=sys.stderr)

    print(report(score(rows, results), results, rows, args.model))


if __name__ == "__main__":
    main()
