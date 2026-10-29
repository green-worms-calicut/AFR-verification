"""Check a whole trip's proof images against what was typed in: loading photos, unloading photos and weigh bills.

A trip runs one of two ways, worked out from which weighing came first:
  - tare first: tare -> loading -> gross -> unloading. The empty vehicle is weighed (tare), loaded and
    photographed, weighed loaded (gross), and photographed again where it unloads;
  - gross first: loading -> gross -> unloading -> tare. The vehicle is loaded and photographed, weighed
    loaded at the plant's weighbridge (gross), unloaded and photographed, then weighed empty (tare).

Photos (each loading and unloading photo): does it show a vehicle, is a GPS stamp printed on it, is
it a photo taken directly rather than a photo of a screen, and does one photo of each kind clearly
show the typed vehicle number? Weigh bills: is there a weighbridge slip (handwritten notes are
ignored), a final slip with both weights, the typed vehicle number on every slip, the typed
quantity as the final slip's net weight, and gross heavier than tare?

Times, from the final slip's tare and gross times and the photos' GPS stamps:
  - tare first: the weighings at least `min_gap_tare_first` apart, every loading photo between them,
    and every unloading photo after gross;
  - gross first: the weighings at least `min_gap_gross_first` apart, every loading photo before gross,
    and every unloading photo between gross and tare.
A photo's clock may be off the weighbridge's by up to `clock_slack`. Weighings on different days may be
at most `max_overnight_gap` apart (a trip across midnight); further, the tare is a stored one.

Place: when the plant's location is given (`--plant`, never fixed in code), the latitude and longitude
on each unloading photo's GPS stamp are read to two decimals, and at least one must place the photo
within `plant_radius` of the plant; any clearly farther is flagged. Each run can change these limits (`Limits`).

Verra, for a Verra project trip (`--verra`): the final slip must have a material entered and carry the
weighbridge's own ink stamp. Our company's name (`OUR_COMPANY`) is sent so that our company's stamp, often on
the slip too, isn't taken for it. These questions are only asked on a Verra project trip.

Across trips: an
image already used in another trip is flagged (`ImageIndex`), and so is a vehicle on two trips at once
(`overlap_checks`).

The checks fall into three parts (`PARTS`, `PART_OF`), each decided on its own:
  - real: did the trip happen as claimed? The plates and bills match the typed vehicle number, the
    weighings and photos are in order and far enough apart, the photos were taken directly, unloading
    was at the plant, and no image or trip is a duplicate;
  - docs: is the proof complete? Every kind of image added, a vehicle and a GPS stamp in every photo,
    a final slip, its net weight the typed quantity, and gross heavier than tare;
  - verra: what a Verra project trip also needs: the material and the weighbridge's stamp.

Nothing is rejected here. A check that finds a problem flags its part, and the trip, an unsure one asks
a person to look, and the person accepts or rejects each part.

Jev can't read text out, it only picks from answers it is given, so the typed vehicle number and
quantity go into the state and Jev judges whether the plate or bill shows them. Each part of a time
(hour, minute, day, month) or of a GPS stamp's latitude and longitude (degrees and the first two
decimals) is a separate Choice question, assembled here. If Jev is sure an image
is turned, it is rotated upright and asked again. Code makes every decision from the answers
(`checks_for`, `trip_checks`, `verdict`); the thresholds (in trip_rules) are placeholders until measure_accuracy.py
and check_trip_folders.py have measured accuracy on real images.

Modules, in the order they build on each other:
  - questions: what Jev is asked about each image;
  - images: reading, sizing and cutting images, trip folders, fingerprints;
  - trip_rules: every check, its part, and the limits;
  - inputs: what people type in, checked and made uniform;
  - verdict: each part's verdict and the trip's;
  - ask_jev: asking Jev about each image and checking a whole trip (`analyze_trip`);
  - __main__: the command line.

Usage:
    .venv/bin/python -m proof_check KL57AB8815 --trip trips/.../171773 --quantity 1630
    .venv/bin/python -m proof_check KL57AB8815 --loading c1.jpg c2.jpg --unloading u1.jpg u2.jpg \
        --bill slip1.jpg slip2.jpg --quantity 1630 [--plant "11.4404, 75.9420" --plant-radius 500] [--verra] \
        [--min-gap-tare-first 60] [--min-gap-gross-first 10] [--clock-slack 5] [--model jev-preview]

The same checks run behind the test page (tools/test_page), the trip-folder runner
(tools/check_trip_folders.py), the accuracy report (tools/measure_accuracy.py) and the gw_sales verifier (verifier/).
"""

# Every module's public names, so `proof_check.analyze_trip` and the like work from the package.
from proof_check.questions import (
    NOT_SHOWN, SLOTS, options, orientation, HOURS, MINUTES, DAYS, MONTHS, TO_24H, STAMP, NO_STAMP_TIME,
    NO_STAMP_DATE, GROSS, TARE, TIME_IN, TIME_OUT, PRINTED, NOT_CAMERA, NO_BILL_TIME, NO_BILL_DATE, SAME_NUMBER,
    bill_time, coordinate, BOX_MODEL, LINE, PICKS, pick, starts_of, ROTATION, QUESTIONS, PLACE, SEAL, VERRA,
    OUR_COMPANY, WEIGHBRIDGE, REPORT, questions_for, question_version, SLIP
)
from proof_check.images import (
    MIN_SIDE, MAX_SIDE, ROLES, SLOT_OF, ROLE_TITLES, IMAGE_TYPES, fit, Upload, role_of, read_trip_folder, load_bytes,
    load, fingerprints, LOOKS_ALIKE, FEATURELESS, ImageIndex, cut_line, cut_slip
)
from proof_check.trip_rules import (
    CONFIDENT, YES, NO, NOUL_THRESHOLDS, Limits, LIMIT_NAMES, LIMIT_UNITS, NOUL_CHECKS, CHOICE_CHECKS, FINAL_ONLY,
    GROUPS, PARTS, PART_OF, readings_for, position, to_24h, same_hour, lower_first, unsure, noul_status, checks_for,
    YEAR, moment, minutes_between, duration, show, weighings, first_slip, check, names, named, plural, EACH_PHOTO,
    each_photo, plate_check, photo_checks, metres_between, distance, from_square, chance_away, plant_check, near_check,
    final_slip, LOOK_FOR, choice_check, seal_checks, verra_checks, bill_checks, weighbridge_check, photo_moments,
    busy_time,
    overlap_minutes, photo_window,
    photo_timing, same_day, trip_checks, short_trip, and_list, reuse_check, overlap_checks
)
from proof_check.inputs import normalize_vehicle_number, parse_limits, parse_quantity, parse_plant, parse_switch
from proof_check.verdict import TITLES, timeline, decide, decide_parts, verdict, add_checks
from proof_check.ask_jev import (
    PRICE_PER_INPUT_TOKEN, HEADERS, TIMEOUT, ROTATE, SIDEWAYS, KEY_LOOKUP, KEY_STORE_COMMAND, NO_KEY, api_key, ask,
    top, summary, TURNING, answer_key, result_from, turn_for, find_slip, read_line, analyze, analyze_trip
)
