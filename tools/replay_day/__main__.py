"""Check a real gw_sales day with the verifier's own code, without the webhook and without sending anything.

Commands (a day is written 2026-11-01):
  download <export.json>   save the export and its images (free; within 100 min of exporting)
  check <day>              check the day's disposals with Jev (paid: shows the cost, asks yes or no)
  recheck <day>            check the day again from saved answers, after a rule change (free)
  report <day>             open the report in the browser, to read and mark (free; Ctrl+C to stop)
  report <day> --save      write a read-only copy to eval/replay-<day>.html instead
  days                     list the saved days: disposals, images missing, checked, reviewed

Examples:
  .venv/bin/python -m tools.replay_day download ~/Downloads/disposals-2026-11-01.json
  .venv/bin/python -m tools.replay_day check 2026-11-01
  .venv/bin/python -m tools.replay_day report 2026-11-01

Images and disposals are kept in trips/gw-<day>/, results and marks in the replay database (REPLAY_DATABASE_URL,
by default verifier_replay on the dev Postgres, port 5433). Verra checks run when VERIFIER_VERRA_PROJECT is set to
the Verra project's code, as in the verifier.
"""

import argparse
import asyncio
import logging
import os
import sys
from collections import Counter
from datetime import date
from pathlib import Path

from tools.replay_day import checking, files, report
from tools.replay_day.files import Problem, command, day_name

EVAL = files.ROOT / "eval"  # gitignored


def a_day(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"write the day as 2026-11-01, not {text!r}") from None


def outcome_counts(outcomes) -> str:
    n = Counter(o.outcome for o in outcomes)
    words = [f"{n['passed']} passed", f"{n['flagged']} flagged"]
    words += [f"{n['error']} error{'' if n['error'] == 1 else 's'}"] if n["error"] else []
    words += [f"{n['kept']} not rechecked"] if n["kept"] else []
    return ", ".join(words)


def run_download(path: Path) -> None:
    export = files.read_export(path)
    done = files.download(export)
    days = sorted(done.days)
    print(f"Done: {sum(done.days.values())} disposals, {done.images} images saved, {len(done.failed)} failed.")
    if len(days) > 1:
        print("They are filed by trip date (arrival, IST), so this export is split over more than one day:")
    for d in days:
        print(f"  {day_name(d)}: {done.days[d]} disposal{'' if done.days[d] == 1 else 's'} in "
              f"{files.day_folder(d).relative_to(files.ROOT)}")
    if done.failed:
        print("The failed images will be flagged as unavailable. To fetch them, export the day again and run download "
              "with the new file: images already saved aren't downloaded again.")
    print("gw_sales says this export isn't complete (more disposals than it holds): export the rest and run download "
          "with it." if done.more_pages else "This export is complete.")
    print(f"You can delete {path} now: the tool keeps its own copy, without the image links.")
    for d in days:
        print(f"Next: {command('check', d.isoformat())}" + ("  (one command per day)" if len(days) > 1 else ""))


async def run_check(day: date, again: bool, yes: bool) -> None:
    bodies = files.load_day(day)
    verra = os.environ.get("VERIFIER_VERRA_PROJECT") or None
    pool = await checking.open_database()
    try:
        done = await checking.checked_ids(pool, bodies)
        todo = bodies if again else [b for b in bodies if b["subject"]["id"] not in done]
        if not todo:
            print(f"All {len(bodies)} disposals of {day_name(day)} are already checked.\n"
                  f"After a rule change, check them again for free: {command('recheck', day.isoformat())}\n"
                  f"Next: {command('report', day.isoformat())}")
            return
        if missing := files.missing_images(day, todo):
            print(f"{len(missing)} image{' was' if len(missing) == 1 else 's were'} not downloaded: "
                  "they will be flagged as unavailable.")
        if not verra:
            print("Verra checks are off: VERIFIER_VERRA_PROJECT (the Verra project's code) isn't set.")
        n, low, high = checking.estimate(todo)
        print(f"{len(todo)} disposal{'' if len(todo) == 1 else 's'} of {day_name(day)}, {n} images to send to Jev.")
        print(f"Estimated cost: about ${(low + high) / 2:.2f} (between ${low:.2f} and ${high:.2f}); "
              "images answered before are free.")
        if not yes and input("Check them now? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Nothing was checked.")
            return
        with checking.jev_client() as client:
            outcomes = await checking.check_day(pool, day, todo, client, verra)
        print(f"Done: {outcome_counts(outcomes)}. Cost ${sum(o.cost for o in outcomes):.2f}.")
        if any(o.outcome == "error" for o in outcomes):
            print(f"Disposals with an error are tried again by: {command('check', day.isoformat())}")
        print(f"Next: {command('report', day.isoformat())}")
    finally:
        await pool.close()


async def run_recheck(day: date) -> None:
    bodies = files.load_day(day)
    pool = await checking.open_database()
    try:
        done = await checking.checked_ids(pool, bodies)
        todo = [b for b in bodies if b["subject"]["id"] in done]
        if not todo:
            raise Problem(f"No disposal of {day_name(day)} is checked yet: {command('check', day.isoformat())}")
        print(f"Checking {len(todo)} disposals of {day_name(day)} again from saved answers (no Jev requests, free).")
        with checking.saved_only_client() as client:
            outcomes = await checking.check_day(pool, day, todo, client, os.environ.get("VERIFIER_VERRA_PROJECT") or None,
                                                saved_only=True)
        print(f"Done: {outcome_counts(outcomes)}.")
        if kept := [o for o in outcomes if o.outcome == "kept"]:
            print(f"{len(kept)} kept their earlier result: they need Jev answers not saved yet (the questions changed?). "
                  f"Ask Jev for just those answers (paid): {command('check', day.isoformat(), '--all')}")
        print(f"Next: {command('report', day.isoformat())}")
    finally:
        await pool.close()


def run_report(day: date, save: bool, port: int, margin: float, sample: int) -> None:
    from tools.replay_day import server
    source = server.Report(day, margin, sample)
    if not save:
        server.serve(source, port)
        return
    try:
        data = source.data()
        report.embed_images(data, day)
        EVAL.mkdir(exist_ok=True)
        path = EVAL / f"replay-{day.isoformat()}.html"
        path.write_text(report.static_page(data))
        print(f"Saved {path.relative_to(files.ROOT)} (read-only: marks are made on {command('report', day.isoformat())}).")
    finally:
        source.close()


async def run_days() -> None:
    days = files.saved_days()
    if not days:
        print(f"No days saved yet. Start with: {command('download', '~/Downloads/<file>.json')}")
        return
    try:
        pool = await checking.open_database()
    except Problem as problem:
        pool = None
        print(problem)
    print(f"{'Day':<12}  {'Disposals':>9}  {'Images missing':>14}  {'Checked':>8}  {'Reviewed':>8}")
    try:
        for day in days:
            bodies = files.load_saved(files.day_folder(day)).values()
            bodies = sorted(bodies, key=files.entered)
            checked = reviewed = "?"
            if pool:
                data = report.build(day, bodies, await checking.results(pool, bodies), await checking.marks(pool))
                s = data["summary"]
                checked = f"{len(bodies) - s['outcomes'].get('unchecked', 0)}/{len(bodies)}"
                reviewed = f"{s['reviewed']}/{s['toReview']}"
            print(f"{day_name(day):<12}  {len(bodies):>9}  {len(files.missing_images(day, bodies)):>14}  {checked:>8}  {reviewed:>8}")
    finally:
        if pool:
            await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m tools.replay_day", description=__doc__.split("\n\n")[0],
                                     epilog=__doc__.split("\n\n", 1)[1], formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", metavar="command")
    p = commands.add_parser("download", help="save an export and its images (free)")
    p.add_argument("export", type=Path, help="the JSON file downloaded from gw_sales")
    p = commands.add_parser("check", help="check a day with Jev (paid: asks first)")
    p.add_argument("day", type=a_day, help="such as 2026-11-01")
    p.add_argument("--all", action="store_true", help="check every disposal again, asking Jev only what isn't saved")
    p.add_argument("--yes", action="store_true", help="don't ask before sending to Jev")
    p = commands.add_parser("recheck", help="check a day again from saved answers (free)")
    p.add_argument("day", type=a_day, help="such as 2026-11-01")
    p = commands.add_parser("report", help="open a day's report to read and mark (free)")
    p.add_argument("day", type=a_day, help="such as 2026-11-01")
    p.add_argument("--save", action="store_true", help="write a read-only copy to eval/replay-<day>.html instead")
    p.add_argument("--port", type=int, default=8766)
    p.add_argument("--margin", type=float, default=report.MARGIN, help="how close above its threshold is a risky pass")
    p.add_argument("--sample", type=int, default=report.SAMPLE, help="other passed disposals picked at random")
    commands.add_parser("days", help="list the saved days")
    args = parser.parse_args()
    logging.basicConfig(level=logging.ERROR, format="%(message)s")  # what was skipped shows in the report instead
    try:
        if args.command == "download":
            run_download(args.export)
        elif args.command == "check":
            asyncio.run(run_check(args.day, args.all, args.yes))
        elif args.command == "recheck":
            asyncio.run(run_recheck(args.day))
        elif args.command == "report":
            run_report(args.day, args.save, args.port, args.margin, args.sample)
        elif args.command == "days":
            asyncio.run(run_days())
        else:
            parser.print_help()
    except Problem as problem:
        sys.exit(str(problem))
    except KeyboardInterrupt:
        sys.exit("\nStopped. What was done so far is saved; run the same command again to go on.")


if __name__ == "__main__":
    main()
