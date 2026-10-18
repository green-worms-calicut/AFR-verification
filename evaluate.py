"""Measure how accurate Jev is on your own labeled images, and choose confidence thresholds from it.

1. Copy labels.example.csv to labels.csv. Add one row per image with the correct answers
   (file paths are relative to the CSV). Leave a cell blank to skip it, or write not_shown
   if that value isn't on the image.
2. Ask Jev about every image. This costs tokens, and saves the answers to eval/<model>.jsonl:
       .venv/bin/python evaluate.py labels.csv
3. Re-score the saved answers for free, for example after editing labels:
       .venv/bin/python evaluate.py labels.csv --cached

The report shows accuracy per field, accuracy at each confidence level, and for each candidate
threshold how many answers would be accepted automatically and how many of those would be wrong.
"""

import argparse
import csv
import json
import sys
from pathlib import Path

from PIL import Image as PILImage
from PIL import ImageOps
from typesafe_sdk import TypeSafeClient

import check_proof

FIELDS = {  # label column -> group
    "kind": "kind",
    "time_in": "times", "time_out": "times", "photo_time": "times",
    "bill_date": "dates", "photo_date": "dates",
}
BUCKETS = [(0.0, 0.5), (0.5, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 0.95), (0.95, 1.01)]
THRESHOLDS = [0.5, 0.6, 0.7, 0.8, 0.9, 0.95]
SMALL_SAMPLE = 30  # below this many answers, accuracy figures are rough


def normalize(field: str, value: str) -> str:
    """Write labels and answers the same way: 3:09 -> 03:09, 15/10/2026 -> 15/10."""
    value = value.strip().lower()
    if value in ("", check_proof.NOT_SHOWN) or field == "kind":
        return value
    sep = ":" if field.endswith("time") or field.startswith("time") else "/"
    return sep.join(part.zfill(2) for part in value.replace(".", sep).split(sep)[:2])


def load_labels(path: Path) -> list[dict]:
    with path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        row["path"] = (path.parent / row["file"]).resolve()
        if row.get("kind", "").strip() not in check_proof.QUESTIONS["kind"].criteria:
            sys.exit(f"{path}: {row['file']}: kind must be bill, geotagged_photo or other, not {row.get('kind')!r}.")
    return rows


def run(client: TypeSafeClient, rows: list[dict], model: str) -> dict[str, dict]:
    results = {}
    for i, row in enumerate(rows, 1):
        print(f"[{i}/{len(rows)}] {row['file']}", file=sys.stderr)
        try:
            with PILImage.open(row["path"]) as original:
                img = ImageOps.exif_transpose(original).convert("RGB")
            results[row["file"]], _ = check_proof.analyze(client, img, model)
        except Exception as error:  # keep going; the report lists what failed
            print(f"    failed: {type(error).__name__}: {error}", file=sys.stderr)
    return results


def score(rows: list[dict], results: dict[str, dict]) -> list[dict]:
    """One item per labeled cell: what was expected, what Jev answered, and how confident it was.

    Readings are put together for the labeled kind, so a wrong kind answer doesn't hide how well
    the times and dates were read (every image is asked every question).
    """
    items = []
    for row in rows:
        result = results.get(row["file"])
        if result is None:
            continue
        expected_kind = normalize("kind", row["kind"])
        readings = {r["key"]: r for r in check_proof.readings_for(expected_kind, result["answers"])}
        for field, group in FIELDS.items():
            expected = normalize(field, row.get(field) or "")
            if not expected:
                continue
            if field == "kind":
                got, confidence, detail = result["kind"]["choice"], result["kind"]["confidence"], result["kind"]["top"][:3]
            elif field in readings:
                r = readings[field]
                got, confidence = (check_proof.NOT_SHOWN if r["missing"] else r["value"]), r["min_confidence"]
                detail = [(p["id"], p["choice"], round(p["confidence"], 2)) for p in r["parts"]]
            else:
                continue  # a bill field labeled on a photo, or similar: nothing to compare
            items.append({"file": row["file"], "field": field, "group": group, "expected": expected,
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
    failed = [row["file"] for row in rows if row["file"] not in results]
    out.append(f"Model {model}: {len(results)} of {len(rows)} images checked, {tokens:,} input tokens, ${cost:.4f}")
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
        out.append(table(["file", "field", "expected", "got", "confidence", "detail"], [
            [i["file"], i["field"], i["expected"], i["got"], f"{i['confidence']:.2f}", i["detail"]] for i in wrong
        ]))
    else:
        out.append("None.")
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("labels", type=Path, help="CSV of images and their correct answers")
    parser.add_argument("--model", default="jev-latest", help="jev-latest (1.13), jev-preview (1.14), or a pinned ID")
    parser.add_argument("--cached", action="store_true", help="re-score saved answers instead of asking Jev again")
    args = parser.parse_args()

    rows = load_labels(args.labels)
    cache = Path(__file__).parent / "eval" / f"{args.model}.jsonl"
    if args.cached:
        if not cache.is_file():
            sys.exit(f"No saved answers at {cache}. Run without --cached first.")
        results = {r["file"]: r["result"] for r in map(json.loads, cache.read_text().splitlines())}
    else:
        with TypeSafeClient(headers=check_proof.HEADERS) as client:
            results = run(client, rows, args.model)
        cache.parent.mkdir(exist_ok=True)
        cache.write_text("".join(json.dumps({"file": f, "result": r}) + "\n" for f, r in results.items()))
        print(f"Saved answers to {cache}", file=sys.stderr)

    print(report(score(rows, results), results, rows, args.model))


if __name__ == "__main__":
    main()
