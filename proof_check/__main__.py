"""Check one trip's images from the command line (see the package's docstring for the options).

Usage:
    .venv/bin/python -m proof_check KL57AB8815 --trip trips/.../171773 --quantity 1630
"""

import argparse
import sys
from dataclasses import fields
from datetime import date
from pathlib import Path

from typesafe_sdk import TypeSafeClient

from proof_check.questions import VERRA
from proof_check.images import ImageIndex, ROLES, ROLE_TITLES, Upload, read_trip_folder
from proof_check.trip_rules import GROUPS, LIMIT_NAMES, LIMIT_UNITS, Limits, PARTS, distance, duration
from proof_check.inputs import normalize_vehicle_number, parse_limits, parse_plant, parse_quantity
from proof_check.ask_jev import HEADERS, NO_KEY, PRICE_PER_INPUT_TOKEN, TIMEOUT, analyze_trip, api_key

MARK = {"pass": "ok  ", "fail": "FLAG", "review": "??  "}


def print_trip(trip: dict, vehicle_number: str, limits: Limits, plant: tuple[float, float] | None = None,
               verra: bool = False) -> None:
    for role in ROLES:
        for r in trip["images"][role]:
            rotated = f", rotated {r['rotated']}°" if r["rotated"] else ""
            same = f", same file as {r['same_as']}" if r["same_as"] else ""
            print(f"\n{ROLE_TITLES[role]}: {r['name']}  (answered by {r['model']}{rotated}{same})")
            for c in r["checks"]:
                print(f"  {MARK[c['status']]} {c['label']:<26} {c['detail']}  ({c['certainty']:.2f})")
            for reading in r["readings"]:
                if not reading["missing"]:
                    print(f"       {reading['label']:<26} {reading['value']}  ({reading['min_confidence']:.2f})")
    for u in trip.get("unanswered", []):
        print(f"\n{ROLE_TITLES[u['role']]}: {u['name']}  (Jev couldn't answer: {u['why']})")
    v = trip["verdict"]
    tokens = sum(r["input_tokens"] for rs in trip["images"].values() for r in rs)
    print(f"\nVehicle {vehicle_number}: {v['title']}")
    near = f", unloading stamped within {distance(limits.plant_radius)} of the plant at {plant[0]}, {plant[1]}" if plant else ""
    print(f"  Limits: tare first, tare to gross at least {duration(limits.min_gap_tare_first)}; gross first, gross to tare "
          f"at least {duration(limits.min_gap_gross_first)}; clock slack {duration(limits.clock_slack)}{near}")
    if verra:
        print("  Verra project: the final slip must have a material entered and carry the weighbridge's stamp")
    print("  Timeline: " + " -> ".join(f"{e['what']} {e['time']}{' (!)' if e['bad'] else ''}" for e in v["timeline"]))
    for part, p in v["parts"].items():
        print(f"  {PARTS[part]}: {p['title']}")
        shown = [c for c in p["checks"] if c["status"] == "pass" and (c["group"] == "times" or c["key"] in ("unloading_plant", *VERRA))]
        for c in p["flags"] + shown:
            print(f"    {MARK[c['status']]} {GROUPS[c['group']]} · {c['label']}: {c['detail']}")
    print(f"  {tokens} input tokens ≈ ${tokens * PRICE_PER_INPUT_TOKEN:.5f}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("vehicle_number", help="the trip's vehicle number, such as KL05AF9341")
    parser.add_argument("--trip", type=Path, help="a trip folder: collection*, unloading* and weighbill* images")
    parser.add_argument("--loading", type=Path, nargs="+", default=[], help="loading photos, with GPS stamps")
    parser.add_argument("--unloading", type=Path, nargs="+", default=[], help="unloading photos, with GPS stamps")
    parser.add_argument("--bill", type=Path, nargs="+", default=[], help="weighbridge slips")
    parser.add_argument("--quantity", help="the quantity in kilograms, such as 2570, which the final slip's net weight must match")
    parser.add_argument("--plant", help='the plant\'s latitude, longitude, such as "11.4404, 75.9420": unloading photos '
                                        "must be stamped near it. Without it, where they were taken isn't checked.")
    parser.add_argument("--verra", action="store_true", help="a Verra project trip: the final slip must have a material entered and carry the weighbridge's stamp")
    for f in fields(Limits):
        parser.add_argument(f"--{f.name.replace('_', '-')}", dest=f.name, metavar={"minutes": "MIN", "metres": "M"}[LIMIT_UNITS[f.name]],
                            help=f"{LIMIT_NAMES[f.name]}, in {LIMIT_UNITS[f.name]} (default {f.default})")
    parser.add_argument("--others", type=Path, help="a folder of other trips, to flag images used again (such as trips/)")
    parser.add_argument("--model", default="jev-latest", help="jev-latest (1.13), jev-preview (1.14), or a pinned ID")
    args = parser.parse_args()

    number = normalize_vehicle_number(args.vehicle_number)
    if not number:
        parser.error(f"{args.vehicle_number!r} doesn't look like a vehicle number. Use 4 to 12 letters and digits.")
    try:
        quantity = parse_quantity(args.quantity)
        plant = parse_plant(args.plant)
        limits = parse_limits(vars(args))
    except ValueError as error:
        parser.error(str(error))
    uploads = read_trip_folder(args.trip) if args.trip else {role: [] for role in ROLES}
    for role in ROLES:
        uploads[role] += [Upload(p.name, p.read_bytes()) for p in getattr(args, role)]
    if not any(uploads.values()):
        parser.error("Give --trip, or images with --loading, --unloading and --bill.")
    index = ImageIndex.of_folders(args.others) if args.others else None
    trip_id = args.trip.resolve().relative_to(args.others.resolve()).as_posix() \
        if args.trip and args.others and args.trip.resolve().is_relative_to(args.others.resolve()) else None

    if not (key := api_key()):
        sys.exit(NO_KEY)
    with TypeSafeClient(api_key=key, headers=HEADERS, timeout=TIMEOUT) as client:
        try:
            trip, _ = analyze_trip(client, uploads, number, args.model, quantity, limits, index, trip_id, plant=plant,
                                   verra=args.verra, around=date.today())
        except Exception as error:
            sys.exit(f"{type(error).__name__}: {error}")
    print_trip(trip, number, limits, plant, args.verra)


if __name__ == "__main__":
    main()
