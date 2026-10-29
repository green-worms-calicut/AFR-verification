"""Offline tests: run proof_check, tools/check_trip_folders.py, tools/measure_accuracy.py and the test page server against a fake Jev API. No API key or network needed.

Run:
    .venv/bin/python -m unittest discover -s tests -v
"""

import base64
import io
import json
import os
import random
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from compression import zstd
from pathlib import Path
from unittest import mock

import httpx2
from PIL import Image as PILImage
from PIL import ImageDraw

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proof_check  # noqa: E402
from proof_check import Limits, Upload  # noqa: E402
from tools import check_trip_folders, measure_accuracy  # noqa: E402
from tools.test_page import server as test_page_server  # noqa: E402
from typesafe_sdk import Noul, TypeSafeClient  # noqa: E402

NUMBER = "KL05AF9341"
QUANTITY = "2570"  # the sample bill's net weight in kg


def fake_answers(slot: str, picks: dict, body: bytes | None = None) -> dict:
    """A System One response body for one slot's questions, or only those asked in the request `body`.

    `picks` gives a yes probability per yes/no question and a (choice, confidence) per Choice.
    Anything not picked gets an unsure answer: 0.5, or the first option at confidence 0.3.
    """
    answers = {}
    for qid, question in proof_check.QUESTIONS[slot].items():
        if body is not None and f'"{qid}":{{'.encode() not in body:
            continue
        if isinstance(question, Noul):
            answers[qid] = {"type": "noul", "noul": picks.get(qid, 0.5)}
            continue
        options = list(question.criteria)
        choice, conf, *others = picks.get(qid, (options[0], 0.3))
        others = others[0] if others else {}  # probabilities of other options, if given
        rest = (1 - conf - sum(others.values())) / (len(options) - 1 - len(others))
        answers[qid] = {
            "type": "choice",
            "choice": choice,
            "confidence": conf,
            "probabilities": {o: conf if o == choice else others.get(o, rest) for o in options},
        }
    # The stamp's Lat/Long line (a Box question), and the four-decimal values picked from it.
    if body is not None and b'"line":{"type":"box"' in body:
        answers["line"] = {"type": "box", "box": picks.get("line", {"top": 0.3, "bottom": 0.7, "left": 0.2, "right": 0.8}),
                           "confidence": 0.9}
    for qid in proof_check.PICKS:
        if body is not None and f'"{qid}":{{'.encode() in body:
            choice, conf = picks.get(qid, (proof_check.NOT_SHOWN, 0.3))
            answers[qid] = {"type": "choice", "choice": choice, "confidence": conf, "probabilities": {choice: conf, "other": 1 - conf}}
    return {"model": "jev-1.13.0", "answers": answers, "usage": {"input_tokens": 1000, "output_tokens": 30}}


def stamp(hhmm: str, ddmm: str = "15/10", confidence: float = 0.95) -> dict:
    """Picks for a photo's GPS stamp time."""
    (hour, minute), (day, month) = hhmm.split(":"), ddmm.split("/")
    return {"stamp_hour": (hour, confidence), "stamp_minute": (minute, confidence),
            "stamp_day": (day, confidence), "stamp_month": (month, confidence)}


def at(prefix: str, hhmm: str, ddmm: str = "15/10", confidence: float = 0.95, ampm: str = "none") -> dict:
    """Picks for one of the bill's dates and times, such as at("gross", "03:30"), or at("gross", "03:30", ampm="pm")."""
    (hour, minute), (day, month) = hhmm.split(":"), ddmm.split("/")
    return {f"{prefix}_hour": (hour, confidence), f"{prefix}_minute": (minute, confidence), f"{prefix}_ampm": (ampm, confidence),
            f"{prefix}_day": (day, confidence), f"{prefix}_month": (month, confidence)}


def stamped_at(lat: str, long: str, confidence: float = 0.95, picked: float | None = 0.95) -> dict:
    """Picks for a photo's GPS stamp position, such as stamped_at("11.440404", "75.941976"): two decimals,
    then the four-decimal values picked from its line (with confidence `picked`, or none picked).
    """
    picks = {}
    for prefix, value in (("lat", lat), ("long", long)):
        degrees, decimals = value.split(".")
        picks |= {f"{prefix}_degrees": (degrees, confidence), f"{prefix}_decimals": (decimals[:2], confidence)}
        if picked is not None:
            picks[f"{prefix}_pick"] = (f"{degrees}.{decimals[:4]}", picked)
    return picks


PLANT = (11.4404, 75.942)  # a plant given as input; the tests' photos are stamped near it or far from it
AT_PLANT = stamped_at("11.440404", "75.941976")  # picked as 11.4404, 75.9419: the plant is on that square's edge
PULATH = stamped_at("11.164949", "76.154944")  # 38 km away


def not_printed(*prefixes: str) -> dict:
    return {f"{prefix}_{part}": ("not_shown", 0.9) for prefix in prefixes for part in ("hour", "minute", "ampm", "day", "month")}


PHOTO = {
    "orientation": ("upright", 0.97), "shows_vehicle": 0.97, "gps_stamp": 0.95, "screen_photo": 0.03,
    "plate": ("readable", 0.9), "plate_number": ("same", 0.88), **stamp("04:09"),
}
# A good trip: tare 02:00, loading photos 02:40, gross 03:30, unloading photos 04:09.
LOADING = {**PHOTO, **stamp("02:40")}
UNLOADING = PHOTO
BILL_CHECKS = {"orientation": ("upright", 0.95), "weigh_bill": 0.96, "screen_photo": 0.03, "bill_number": ("same", 0.9)}
STAMPED = {"weighbridge_seal": 0.9, "weighbridge_name": ("same", 0.9)}  # the weighbridge's stamp clearly on the slip
FINAL = {**BILL_CHECKS, **STAMPED, "slip_kind": ("both_weights", 0.92), "net_weight": ("same", 0.9), "material": ("filled", 0.9),
         "gross_vs_tare": ("gross_heavier", 0.9), **at("tare", "02:00"), **at("gross", "03:30"),
         **not_printed("in", "out"), "first_weight": ("not_shown", 0.9)}
FIRST = {**BILL_CHECKS, "slip_kind": ("one_weight", 0.9), "net_weight": ("different", 0.9),
         "gross_vs_tare": ("not_shown", 0.85), **at("tare", "02:00"), **not_printed("gross", "in", "out"),
         "first_weight": ("not_shown", 0.9)}
TALLY = {"orientation": ("upright", 0.9), "weigh_bill": 0.04, "slip_kind": ("not_shown", 0.9),
         "bill_number": ("not_shown", 0.9), **not_printed("gross", "tare", "in", "out")}
# Only Time In and Time Out, with the tare taken at Time In.
IN_OUT_FINAL = {**FINAL, **not_printed("gross", "tare"), **at("in", "02:00"), **at("out", "03:30"),
                "first_weight": ("tare", 0.92)}
# Lakshmi: no time beside the weights, one printed at the top of each slip: the first slip at the tare, the final at the gross.
LAKSHMI_FIRST = {**FIRST, **not_printed("tare"), **at("printed", "02:00")}
LAKSHMI_FINAL = {**FINAL, **not_printed("gross", "tare", "in", "out"), **at("printed", "03:30"), "first_weight": ("tare", 0.92)}
SIDEWAYS = {"orientation": ("top_points_right", 0.93)}
NOT_A_VEHICLE = {"orientation": ("upright", 0.9), "shows_vehicle": 0.03, "gps_stamp": 0.04,
                 "plate": ("not_visible", 0.95), "plate_number": ("cannot_tell", 0.9)}

# Each test image is one flat colour, so the fake Jev can tell which image a request is about.
COLOURS = [(200, 30, 30), (30, 200, 30), (30, 30, 200), (200, 200, 30), (200, 30, 200), (30, 200, 200),
           (120, 60, 20), (20, 120, 60), (60, 20, 120), (240, 140, 0), (0, 140, 240), (140, 0, 240)]


def coloured(colour: tuple, quality: int = 90) -> bytes:
    """A test image: one colour in the middle (the fake Jev reads the centre), with a border of grey
    blocks that differs by colour, so each image has its own look-alike fingerprint.
    """
    img = PILImage.new("RGB", (360, 320), colour)
    draw = ImageDraw.Draw(img)
    rng = random.Random(str(colour))
    for row in range(8):
        for col in range(9):
            if row in (0, 1, 6, 7) or col in (0, 1, 7, 8):
                grey = rng.randrange(256)
                draw.rectangle((col * 40, row * 40, col * 40 + 39, row * 40 + 39), fill=(grey, grey, grey))
    buffer = io.BytesIO()
    img.save(buffer, "JPEG", quality=quality)
    return buffer.getvalue()


def sent(request: httpx2.Request) -> bytes:
    """The request body as sent, before the SDK's zstd compression."""
    body = request.read()
    return zstd.decompress(body) if request.headers.get("content-encoding") == "zstd" else body


class FakeJev:
    """Replies to each request with canned answers, and records what was sent.

    Answers are chosen by the image's colour when `by_colour` knows it, otherwise by slot, in order.
    A colour's list is used in turn, and its last answers are reused (an image may be asked twice
    after a turn).
    """

    def __init__(self, photo=(), bill=(), by_colour=None):
        self.responses = {"photo": list(photo), "bill": list(bill)}
        self.by_colour = {c: list(picks) for c, picks in (by_colour or {}).items()}
        self.requests: list[httpx2.Request] = []
        self.slip = {"top": 0.0, "bottom": 1.0, "left": 0.0, "right": 1.0}
        self.lock = threading.Lock()

    def colour_of(self, body: bytes):
        start, end = body.find(b"\xff\xd8\xff"), body.rfind(b"\xff\xd9")
        if start < 0 or end < 0:
            return None
        with PILImage.open(io.BytesIO(body[start:end + 2])) as img:
            px = img.convert("RGB").getpixel((img.width // 2, img.height // 2))
        best = min(self.by_colour, key=lambda c: sum((a - b) ** 2 for a, b in zip(c, px)), default=None)
        return best if best and sum((a - b) ** 2 for a, b in zip(best, px)) < 900 else None

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        with self.lock:
            self.requests.append(request)
            body = sent(request)
            if b'"slip":{"type":"box"' in body:  # where a bill's slip is: the whole image, unless a test says otherwise
                return httpx2.Response(200, json={"model": "jev-1.14.0", "usage": {"input_tokens": 1000, "output_tokens": 4},
                                                  "answers": {"slip": {"type": "box", "box": self.slip, "confidence": 0.95}}})
            slot = "bill" if b"weigh_bill" in body else "photo"
            colour = self.colour_of(body) if self.by_colour else None
            if colour:
                queue = self.by_colour[colour]
                picks = queue.pop(0) if len(queue) > 1 else queue[0]
            else:
                picks = self.responses[slot].pop(0)
        return httpx2.Response(200, json=fake_answers(slot, picks, body))

    def client(self) -> TypeSafeClient:
        return TypeSafeClient(api_key="test", headers=proof_check.HEADERS, transport=httpx2.MockTransport(self))


def sample(name: str) -> PILImage.Image:
    return PILImage.open(ROOT / "examples" / "images" / name).convert("RGB")


def analyze(slot: str, *responses: dict, image: str | None = None, number: str = NUMBER, quantity: str | None = QUANTITY):
    jev = FakeJev(**{slot: responses})
    with jev.client() as client:
        result, sent_img = proof_check.analyze(client, slot, sample(image or f"{slot}.png"), number, "jev-latest", quantity)
    return result, sent_img, jev


def by_key(items: list[dict]) -> dict[str, dict]:
    return {i["key"]: i for i in items}


def make_trip(loading=(LOADING, LOADING), unloading=(UNLOADING, UNLOADING), bill=(FIRST, FINAL), same=()):
    """Uploads for a trip, one colour per image, and the fake Jev that answers for them.

    Each item is picks, or a list of picks for an image asked more than once. `same` lists
    (role, index, role, index) pairs: the second image is the same file as the first.
    """
    uploads, by_colour, n = {}, {}, 0
    for role, items, prefix in (("loading", loading, "collection"), ("unloading", unloading, "unloading"),
                                ("bill", bill, "weighbill")):
        uploads[role] = []
        for i, picks in enumerate(items):
            colour = COLOURS[n]
            n += 1
            by_colour[colour] = picks if isinstance(picks, list) else [picks]
            uploads[role].append(Upload(f"{prefix}{i + 1}.jpg", coloured(colour)))
    for role_a, i, role_b, j in same:
        uploads[role_b][j] = Upload(uploads[role_b][j].name, uploads[role_a][i].data)
    return uploads, FakeJev(by_colour=by_colour)


def run_trip(quantity=QUANTITY, limits=Limits(), index=None, trip_id=None, cache=None, plant=None, verra=False,
             weighbridge=None, **kw):
    uploads, jev = make_trip(**kw)
    with jev.client() as client:
        trip, _ = proof_check.analyze_trip(client, uploads, NUMBER, "jev-latest", quantity, limits, index, trip_id, cache, plant,
                                           verra, weighbridge=weighbridge)
    return trip, jev


class ApiKeyTests(unittest.TestCase):
    def setUp(self):
        proof_check.ask_jev._keyring_key = None
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(setattr, proof_check.ask_jev, "_keyring_key", None)
        os.environ.pop("TYPESAFE_API_KEY", None)

    def test_environment_variable_comes_first(self):
        os.environ["TYPESAFE_API_KEY"] = "from-env"
        with mock.patch("subprocess.run") as run:
            self.assertEqual(proof_check.api_key(), "from-env")
        run.assert_not_called()

    def test_reads_the_keyring_once(self):
        found = mock.Mock(stdout="from-keyring\n")
        with mock.patch("subprocess.run", return_value=found) as run:
            self.assertEqual(proof_check.api_key(), "from-keyring")
            self.assertEqual(proof_check.api_key(), "from-keyring")
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], proof_check.KEY_LOOKUP)

    def test_no_key_when_not_stored_or_no_keyring(self):
        with mock.patch("subprocess.run", return_value=mock.Mock(stdout="")):
            self.assertIsNone(proof_check.api_key())
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            self.assertIsNone(proof_check.api_key())


class FitTests(unittest.TestCase):
    def test_sizes_stay_within_api_limits(self):
        for size in [(346, 588), (4000, 3000), (100, 100), (3000, 200)]:
            w, h = proof_check.fit(PILImage.new("RGB", size)).size
            with self.subTest(size=size):
                self.assertEqual(max(w, h), proof_check.MAX_SIDE)
                self.assertGreaterEqual(min(w, h), proof_check.MIN_SIDE)


class TypedValueTests(unittest.TestCase):
    def test_normalize_vehicle_number(self):
        self.assertEqual(proof_check.normalize_vehicle_number(" kl 05 af-9341 "), NUMBER)
        self.assertEqual(proof_check.normalize_vehicle_number("KL.05.AF.9341"), NUMBER)
        self.assertIsNone(proof_check.normalize_vehicle_number(""))
        self.assertIsNone(proof_check.normalize_vehicle_number("KL0"))
        self.assertIsNone(proof_check.normalize_vehicle_number("KL05AF9341/2"))

    def test_parse_quantity(self):
        for typed, kg in [("480", "480"), (" 2,570 kg ", "2570"), ("02570", "2570"), ("480.50", "480.5"),
                          ("480.0 KG", "480"), ("0.5", "0.5"), (1630, "1630"), ("", None), (None, None)]:
            with self.subTest(typed=typed):
                self.assertEqual(proof_check.parse_quantity(typed), kg)
        for typed in ("0", "abc", "480 t", "-5", "4 80"):
            with self.subTest(typed=typed), self.assertRaises(ValueError):
                proof_check.parse_quantity(typed)

    def test_parse_switch(self):
        for value, on in [(True, True), ("yes", True), (1, True), ("true", True), (False, False), (None, False), ("", False),
                          ("no", False)]:
            with self.subTest(value=value):
                self.assertEqual(proof_check.parse_switch(value, "verra"), on)
        with self.assertRaises(ValueError):
            proof_check.parse_switch("maybe", "verra")

    def test_parse_limits_keeps_defaults_for_blanks(self):
        self.assertEqual(proof_check.parse_limits({}), Limits(60, 10, 5, 720, 500))
        self.assertEqual(proof_check.parse_limits({"plant_radius": "250"}).plant_radius, 250)
        self.assertEqual(proof_check.parse_limits({"min_gap_tare_first": " 90 ", "clock_slack": "0"}), Limits(90, clock_slack=0))
        for bad in ("five", "-5", "1.5", "99999"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                proof_check.parse_limits({"clock_slack": bad})

    def test_parse_plant(self):
        for typed, plant in [("11.4404, 75.9420", PLANT), ("11.4404,75.942", PLANT), (" 11.4404 75.942 ", PLANT),
                             ("11.4404°, 75.942°", PLANT), ([11.4404, 75.942], PLANT), ("", None), (None, None)]:
            with self.subTest(typed=typed):
                self.assertEqual(proof_check.parse_plant(typed), plant)
        for typed in ("Raroth", "11.4404", "-11.4404, 75.942", "91, 75", "11.44, 181", "11.44 N, 75.94 E"):
            with self.subTest(typed=typed), self.assertRaises(ValueError):
                proof_check.parse_plant(typed)

    def test_a_photo_at_the_plant_is_never_away_for_the_cut_off_digits(self):
        self.assertEqual(proof_check.from_square("11.44, 75.94", PLANT), 0)
        # A plant just inside its square: a photo 50 m away, across the line, reads as the next square.
        self.assertLess(proof_check.from_square("11.43, 75.94", (11.4402, 75.942)), 25)
        self.assertGreater(proof_check.from_square("11.42, 75.92", PLANT), 1500)  # 171493, unloaded 2.9 km away

    def test_distances(self):
        self.assertAlmostEqual(proof_check.metres_between((11.4404, 75.942), (11.4414, 75.942)), 111, delta=1)
        self.assertAlmostEqual(proof_check.metres_between(PLANT, (11.164949, 76.154944)), 38_300, delta=200)
        self.assertEqual([proof_check.distance(m) for m in (40.4, 500, 1234, 38_210)], ["40 m", "500 m", "1.2 km", "38 km"])

    def test_duration(self):
        self.assertEqual([proof_check.duration(m) for m in (0, 13, 60, 75, 348)], ["0 min", "13 min", "1 h", "1 h 15 min", "5 h 48 min"])

    def test_role_of_file_names(self):
        for name, role in [("collection1.jpg", "loading"), ("Loading-2.JPG", "loading"), ("unloading2.jpg", "unloading"),
                           ("weighbill3.heic", "bill"), ("bill.png", "bill"), ("notes.txt", None), ("photo.jpg", None)]:
            with self.subTest(name=name):
                self.assertEqual(proof_check.role_of(name), role)


class AnalyzeTests(unittest.TestCase):
    def test_sideways_bill_is_rotated_and_asked_again(self):
        result, sent_img, jev = analyze("bill", SIDEWAYS, FINAL)
        self.assertEqual(len(jev.requests), 3)  # the slip found, then asked, turned and asked again
        self.assertEqual(jev.requests[0].headers["TypeSafe-Danger-Zone-Images"], "true")
        self.assertEqual(result["rotated"], 90)
        self.assertGreater(sent_img.width, sent_img.height)  # portrait photo of a landscape slip, now landscape
        readings = {r["key"]: r["value"] for r in result["readings"]}
        self.assertEqual((readings["tare_time"], readings["gross_time"], readings["in_time"]), ("02:00", "03:30", None))
        self.assertEqual(result["input_tokens"], 3000)

    def test_a_bill_is_cut_to_its_slip_before_it_is_read(self):
        jev = FakeJev(bill=(FINAL,))
        jev.slip = {"top": 0.25, "bottom": 0.75, "left": 0.0, "right": 1.0}
        img = PILImage.new("RGB", (400, 1200), "white")
        with jev.client() as client:
            result, sent_img = proof_check.analyze(client, "bill", img, NUMBER, "jev-latest", QUANTITY, cache := {}, "k")
        first = sent(jev.requests[0]).replace(b" ", b"")
        self.assertIn(b'"slip":{"type":"box"', first)
        self.assertIn(f'"model":"{proof_check.BOX_MODEL}"'.encode(), first)
        self.assertEqual((result["requests"], result["slip"]["top"]), (2, 0.25))
        # Half the photo's height, with a little room: 400 x 636 before scaling, so wider than half as tall as before.
        self.assertAlmostEqual(sent_img.width / sent_img.height, 400 / 636, places=2)
        # Saved answers cut the image the same way, without asking.
        _, again = proof_check.analyze(None, "bill", img, NUMBER, "jev-latest", QUANTITY, cache, "k")
        self.assertEqual(again.size, sent_img.size)
        # Photos aren't cut.
        _, _, jev = analyze("photo", PHOTO)
        self.assertFalse(any(b'"slip":{"type":"box"' in sent(r) for r in jev.requests))

    def test_vehicle_number_and_quantity_are_sent_in_the_state(self):
        _, _, jev = analyze("photo", PHOTO)
        body = sent(jev.requests[0]).replace(b" ", b"")
        self.assertIn(b'"vehicle_number":"KL05AF9341"', body)
        self.assertNotIn(b"quantity_kg", body)
        _, _, jev = analyze("bill", FINAL)
        body = sent(jev.requests[1]).replace(b" ", b"")  # after the slip is found
        self.assertIn(b'"quantity_kg":"2570"', body)
        self.assertIn(b'"net_weight":{', body)
        result, _, jev = analyze("bill", FINAL, quantity=None)
        self.assertNotIn(b"net_weight", sent(jev.requests[1]))
        self.assertNotIn("net_weight", by_key(result["checks"]))

    def test_surely_sideways_but_unsure_which_way_is_turned_and_checked(self):
        # As the real sideways slips: 55% one way, 40% the other, under 10% upright.
        split = {**FINAL, "orientation": ("top_points_left", 0.4, {"top_points_right": 0.35, "upright": 0.15})}
        result, sent_img, jev = analyze("bill", split, {**FINAL, "orientation": ("upright", 0.9)})
        self.assertEqual((len(jev.requests), result["rotated"]), (3, 270))
        self.assertGreater(sent_img.width, sent_img.height)
        # The guess was the wrong way: the second look says upside down, so it's turned half round and asked again.
        result, _, jev = analyze("bill", split, {**FINAL, "orientation": ("upside_down", 0.85)}, FINAL)
        self.assertEqual((len(jev.requests), result["rotated"], len(result["looks"])), (4, 90, 3))
        # As 171492's slip: upside down only 49% "confident", but far likelier than upright.
        unsure_flip = {**FINAL, "orientation": ("upside_down", 0.49, {"upright": 0.1, "top_points_left": 0.19})}
        result, _, jev = analyze("bill", split, unsure_flip, FINAL)
        self.assertEqual((len(jev.requests), result["rotated"]), (4, 90))

    def test_unsure_orientation_is_not_rotated(self):
        result, _, jev = analyze("photo", {**PHOTO, "orientation": ("top_points_left", 0.4)})
        self.assertEqual((len(jev.requests), result["rotated"]), (1, 0))

    def test_rotates_at_the_lower_rotation_cut_off(self):
        # 0.56 is what the real Jev gave the sideways sample bill: below CONFIDENT, above ROTATE.
        result, _, jev = analyze("bill", {**FINAL, "orientation": ("top_points_right", 0.56)}, FINAL)
        self.assertEqual((len(jev.requests), result["rotated"]), (3, 90))  # the slip found, then two looks

    def test_good_photo_passes_every_check(self):
        result, _, _ = analyze("photo", PHOTO)
        checks = by_key(result["checks"])
        self.assertEqual(list(checks), ["shows_vehicle", "gps_stamp", "screen_photo", "plate", "plate_number"])
        self.assertTrue(all(c["status"] == "pass" for c in checks.values()))
        self.assertEqual(checks["screen_photo"]["detail"], "A photo taken directly")
        self.assertEqual([r["value"] for r in result["readings"]], ["04:09", "15/10"])

    def test_photo_of_a_screen_fails_its_check(self):
        checks = by_key(analyze("photo", {**PHOTO, "screen_photo": 0.92})[0]["checks"])
        self.assertEqual((checks["screen_photo"]["status"], checks["screen_photo"]["detail"]), ("fail", "A photo of a screen"))
        self.assertEqual(by_key(analyze("photo", {**PHOTO, "screen_photo": 0.7})[0]["checks"])["screen_photo"]["status"], "review")
        self.assertEqual(by_key(analyze("photo", {**PHOTO, "screen_photo": 0.4})[0]["checks"])["screen_photo"]["status"], "pass")

    def test_yes_no_thresholds(self):
        for yes, status in [(0.95, "pass"), (0.8, "pass"), (0.5, "review"), (0.2, "fail"), (0.05, "fail")]:
            with self.subTest(yes=yes):
                self.assertEqual(by_key(analyze("photo", {**PHOTO, "gps_stamp": yes})[0]["checks"])["gps_stamp"]["status"], status)

    def test_first_slip_leaves_out_the_final_slip_questions(self):
        checks = by_key(analyze("bill", FIRST)[0]["checks"])
        self.assertEqual(checks["slip_kind"]["detail"], "First slip: one weight only")
        self.assertNotIn("net_weight", checks)
        self.assertNotIn("gross_vs_tare", checks)

    def test_not_shown_part_makes_the_reading_missing(self):
        result, _, _ = analyze("photo", {**PHOTO, "stamp_hour": ("not_shown", 0.9)})
        readings = by_key(result["readings"])
        self.assertTrue(readings["photo_time"]["missing"])
        self.assertIsNone(readings["photo_time"]["value"])
        self.assertFalse(readings["photo_date"]["missing"])


class TripTests(unittest.TestCase):
    def test_good_trip_is_accepted(self):
        trip, jev = run_trip()
        v = trip["verdict"]
        self.assertEqual((v["status"], v["title"], v["flags"]), ("pass", "Accepted", []))
        self.assertEqual(len(jev.requests), 8)
        c = by_key(v["checks"])
        self.assertEqual(c["weigh_order"]["detail"], "Tare first (02:00), then gross (03:30), from the times printed with each weight")
        self.assertEqual(c["weigh_gap"]["detail"], "1 h 30 min apart (at least 1 h)")
        self.assertEqual(c["weigh_day"]["detail"], "Both on 15/10")
        self.assertEqual(c["loading_timing"]["detail"], "Loading 02:40 is between tare 02:00 and gross 03:30")
        self.assertEqual(c["unloading_timing"]["detail"], "Unloading 04:09 is after gross 03:30")
        self.assertEqual(c["bill_found"]["detail"], "weighbill2.jpg: both weights and the net")
        self.assertEqual(c["net_weight"]["detail"], "weighbill2.jpg: Bill shows a net weight of 2570 kg")
        self.assertEqual(c["bill_number"]["detail"], f"All 2 slips show {NUMBER}")
        self.assertEqual(c["loading_plate"]["detail"], "Seen clearly in 2 of 2")
        self.assertEqual([e["what"] for e in v["timeline"]], ["Tare", "Loading photos", "Gross", "Unloading photos"])
        self.assertFalse(any(e["bad"] for e in v["timeline"]))
        self.assertEqual(set(trip["images"]), {"loading", "unloading", "bill"})
        self.assertNotIn("reused", c)  # no index given

    def test_the_first_slips_net_weight_is_not_used(self):
        # FIRST answers "different" for the net weight: only the final slip's counts.
        self.assertEqual(run_trip()[0]["verdict"]["status"], "pass")
        v = run_trip(bill=(FIRST, {**FINAL, "net_weight": ("different", 0.9)}))[0]["verdict"]
        self.assertEqual(v["status"], "problem")
        self.assertEqual(by_key(v["checks"])["net_weight"]["status"], "fail")

    def test_a_handwritten_note_is_ignored(self):
        v = run_trip(bill=(TALLY, FINAL))[0]["verdict"]
        self.assertEqual(v["status"], "pass")
        self.assertEqual(by_key(v["checks"])["bill_found"]["detail"],
                         "weighbill2.jpg: both weights and the net (ignored as not a slip: weighbill1.jpg)")

    def test_no_final_slip_asks_a_person(self):
        v = run_trip(bill=(FIRST,))[0]["verdict"]
        self.assertEqual(v["status"], "review")
        c = by_key(v["checks"])
        self.assertEqual(c["bill_found"]["detail"], "No slip clearly shows both weights and the net weight, so the weighing times "
                         "and net weight couldn't be read. Look for the final slip.")
        self.assertEqual(c["weigh_order"]["status"], "review")

    def test_gross_first_runs_loading_gross_unloading_tare(self):
        # As 171522 on 19 Oct 2026: loading 08:06, gross 08:37, unloading 08:46, tare 09:02.
        gross_first = {**FINAL, **at("gross", "03:30"), **at("tare", "04:15")}
        trip, _ = run_trip(bill=(gross_first,))
        v = trip["verdict"]
        self.assertEqual(v["status"], "pass", v["flags"])
        c = by_key(v["checks"])
        self.assertEqual(c["weigh_order"]["detail"], "Gross first (03:30), then tare (04:15), from the times printed with each weight")
        self.assertEqual((c["weigh_gap"]["label"], c["weigh_gap"]["detail"]), ("Time between weighings", "45 min apart (at least 10 min)"))
        self.assertEqual((c["loading_timing"]["label"], c["loading_timing"]["detail"]), ("Loading photo times", "Loading 02:40 is before gross 03:30"))
        self.assertEqual((c["unloading_timing"]["label"], c["unloading_timing"]["detail"]),
                         ("Unloading photo times", "Unloading 04:09 is between gross 03:30 and tare 04:15"))
        self.assertEqual([e["what"] for e in v["timeline"]], ["Loading photos", "Gross", "Unloading photos", "Tare"])
        # The trip's window is its photos', first loading to last unloading, not its weighings.
        self.assertEqual([t[5:16] for t in v["window"]], ["10-15T02:40", "10-15T04:09"])

    def test_gross_first_weighings_too_close_are_a_problem(self):
        v = run_trip(bill=({**FINAL, **at("gross", "04:05"), **at("tare", "04:12")},))[0]["verdict"]
        self.assertEqual(v["status"], "problem")
        self.assertEqual(by_key(v["checks"])["weigh_gap"]["detail"], "The tare (empty) and gross (loaded) weighings are only 7 min apart, "
                         "but at least 10 min is needed for unloading between them.")
        self.assertTrue(all(e["bad"] for e in v["timeline"] if e["what"] in ("Tare", "Gross")))
        v = run_trip(bill=({**FINAL, **at("gross", "04:05"), **at("tare", "04:12")},), limits=Limits(min_gap_gross_first=5))[0]["verdict"]
        self.assertEqual(v["status"], "pass", v["flags"])

    def test_gross_first_photos_out_of_place_are_a_problem(self):
        gross_first = {**FINAL, **at("gross", "03:30"), **at("tare", "04:15")}
        v = run_trip(loading=({**LOADING, **stamp("04:00")},), bill=(gross_first,))[0]["verdict"]
        self.assertEqual(v["status"], "problem")
        self.assertEqual(by_key(v["checks"])["loading_timing"]["detail"],
                         "collection1.jpg was taken at 04:00, after the gross (loaded) weighing at 03:30. "
                         "Loading photos must come before it (5 min clock difference allowed).")
        v = run_trip(unloading=(UNLOADING, {**UNLOADING, **stamp("04:25")}), bill=(gross_first,))[0]["verdict"]
        self.assertEqual(by_key(v["checks"])["unloading_timing"]["detail"],
                         "unloading2.jpg was taken at 04:25, after the tare (empty) weighing at 04:15. "
                         "Unloading photos must come before it (5 min clock difference allowed).")
        v = run_trip(unloading=({**UNLOADING, **stamp("03:20")},), bill=(gross_first,))[0]["verdict"]
        self.assertEqual(by_key(v["checks"])["unloading_timing"]["detail"],
                         "unloading1.jpg was taken at 03:20, before the gross (loaded) weighing at 03:30. "
                         "Unloading photos must come after it (5 min clock difference allowed).")

    def test_weighings_too_close_are_a_problem(self):
        v = run_trip(bill=(FIRST, {**FINAL, **at("tare", "03:00")}))[0]["verdict"]
        self.assertEqual(v["status"], "problem")
        self.assertEqual(by_key(v["checks"])["weigh_gap"]["detail"], "The tare (empty) and gross (loaded) weighings are only 30 min apart, "
                         "but at least 1 h is needed for loading between them.")

    def test_loading_photo_outside_tare_to_gross_is_a_problem(self):
        early = {**LOADING, **stamp("01:40")}
        v = run_trip(loading=(early, LOADING))[0]["verdict"]
        self.assertEqual(v["status"], "problem")
        self.assertEqual(by_key(v["checks"])["loading_timing"]["detail"],
                         "collection1.jpg was taken at 01:40, before the tare (empty) weighing at 02:00. "
                         "Loading photos must come after it (5 min clock difference allowed).")
        self.assertTrue(next(e for e in v["timeline"] if e["what"] == "Loading photos")["bad"])
        late = {**LOADING, **stamp("03:40")}
        self.assertIn("at 03:40, after the gross (loaded) weighing at 03:30", by_key(run_trip(loading=(LOADING, late))[0]["verdict"]["checks"])["loading_timing"]["detail"])

    def test_clock_slack_allows_a_few_minutes(self):
        self.assertEqual(run_trip(loading=({**LOADING, **stamp("01:56")},))[0]["verdict"]["status"], "pass")
        v = run_trip(loading=({**LOADING, **stamp("01:56")},), limits=Limits(clock_slack=0))[0]["verdict"]
        self.assertEqual(by_key(v["checks"])["loading_timing"]["detail"], "collection1.jpg was taken at 01:56, before the tare (empty) weighing at 02:00. "
                         "Loading photos must come after it.")

    def test_unloading_photo_before_gross_is_a_problem(self):
        v = run_trip(unloading=(UNLOADING, {**UNLOADING, **stamp("03:10")}))[0]["verdict"]
        self.assertEqual(v["status"], "problem")
        self.assertEqual(by_key(v["checks"])["unloading_timing"]["detail"],
                         "unloading2.jpg was taken at 03:10, before the gross (loaded) weighing at 03:30. "
                         "Unloading photos must come after it (5 min clock difference allowed).")

    def test_unloading_must_be_the_same_day_as_gross(self):
        c = by_key(run_trip()[0]["verdict"]["checks"])
        self.assertEqual((c["unloading_day"]["status"], c["unloading_day"]["detail"]), ("pass", "All on 15/10"))
        later = {**UNLOADING, **stamp("04:09", "16/10")}
        v = run_trip(unloading=(UNLOADING, later))[0]["verdict"]
        self.assertEqual(v["status"], "problem")
        self.assertEqual(by_key(v["checks"])["unloading_day"]["detail"], "unloading2.jpg is dated 16/10, but the gross (loaded) weighing was on 15/10. "
                         "Unloading must be on the same day.")
        self.assertTrue(next(e for e in v["timeline"] if e["what"] == "Unloading photos")["bad"])
        unsure = {**UNLOADING, **stamp("04:09", "16/10", confidence=0.6)}
        self.assertEqual(by_key(run_trip(unloading=(unsure,))[0]["verdict"]["checks"])["unloading_day"]["status"], "review")

    def test_a_tare_from_another_day_is_a_problem(self):
        # A stored tare from the day before, not weighed on this trip (27 Oct 2026: 172907, 172940, 173097).
        v = run_trip(bill=(FIRST, {**FINAL, **at("tare", "11:31", "14/10")}))[0]["verdict"]
        self.assertEqual((v["status"], [c["key"] for c in v["flags"]]), ("problem", ["weigh_day"]))
        c = by_key(v["checks"])["weigh_day"]
        self.assertEqual((c["part"], c["images"]), ("real", ["weighbill2.jpg"]))
        self.assertEqual(c["detail"], "The tare (empty) weighing on weighbill2.jpg is dated 14/10, but the gross (loaded) "
                                      "weighing 15/10, 15 h 59 min apart. The tare may be a stored one, not weighed on this trip: check the "
                                      "vehicle was weighed empty for this trip.")
        self.assertTrue(next(e for e in v["timeline"] if e["what"] == "Tare")["bad"])
        unsure = {**FINAL, **at("tare", "11:31", "14/10", confidence=0.6)}
        c = by_key(run_trip(bill=(FIRST, unsure))[0]["verdict"]["checks"])["weigh_day"]
        self.assertEqual((c["status"], c["cause"], c["detail"]),
                         ("review", "weigh_times", "Tare on 14/10, gross on 15/10, if the weighing times are right"))

    def test_an_unreadable_or_unsure_stamp_asks_a_person(self):
        v = run_trip(loading=(LOADING, {**LOADING, "stamp_minute": ("not_shown", 0.9)}))[0]["verdict"]
        self.assertEqual(v["status"], "review")
        self.assertEqual(by_key(v["checks"])["loading_timing"]["detail"],
                         "The time on the GPS stamp of collection2.jpg couldn't be read. Check it was taken between "
                         "the tare (empty) weighing at 02:00 and the gross (loaded) weighing at 03:30.")
        self.assertEqual(by_key(v["checks"])["loading_timing"]["images"], ["collection2.jpg"])
        v = run_trip(unloading=({**UNLOADING, "stamp_minute": ("09", 0.5)},))[0]["verdict"]
        c = by_key(v["checks"])["unloading_timing"]
        self.assertEqual((c["status"], c["images"], c["cause"]), ("review", ["unloading1.jpg"], None))
        self.assertEqual(c["detail"], "The time on the GPS stamp of unloading1.jpg was read as 04:09, but only 50% sure. "
                                      "Check it was taken after the gross (loaded) weighing at 03:30.")

    def test_unsure_weighing_times_are_one_reason(self):
        # The gap and every photo time depend on the weighing times: unsure of those, all ask a person for one cause.
        v = run_trip(bill=(FIRST, {**FINAL, **at("tare", "02:00", confidence=0.6)}))[0]["verdict"]
        flags = v["parts"]["real"]["flags"]
        self.assertEqual([(c["key"], c["cause"]) for c in flags],
                         [(k, "weigh_times") for k in ("weigh_order", "weigh_gap", "weigh_day", "loading_timing",
                                                       "unloading_timing", "unloading_day")])
        self.assertEqual(flags[0]["detail"], "Check the weighing times on weighbill2.jpg: read as tare (empty) 02:00 and gross "
                                             "(loaded) 03:30, from the times printed with each weight, but only 60% sure.")
        self.assertEqual(flags[0]["images"], ["weighbill2.jpg"])
        self.assertEqual(flags[0]["order"], "tare_first")
        self.assertEqual(flags[1]["detail"], "1 h 30 min apart if those times are right (at least 1 h needed)")
        self.assertEqual(flags[2]["detail"], "Both on 15/10, if the weighing times are right")
        self.assertEqual(flags[3]["detail"], "Loading 02:40 is between tare 02:00 and gross 03:30, if the weighing times are right")
        self.assertEqual(flags[5]["detail"], "All on 15/10, if the weighing times are right")

    def test_without_a_plant_where_unloading_happened_is_not_asked(self):
        trip, jev = run_trip()
        self.assertNotIn("unloading_plant", by_key(trip["verdict"]["checks"]))
        self.assertFalse(any(b"lat_degrees" in sent(r) for r in jev.requests))
        self.assertNotIn("position", {x["key"] for x in trip["images"]["unloading"][0]["readings"]})

    def test_material_entered_on_the_final_slip_of_a_verra_project_trip(self):
        trip, jev = run_trip()
        self.assertFalse(any(b'"material":{' in sent(r) for r in jev.requests))
        self.assertNotIn("material", by_key(trip["verdict"]["checks"]))
        trip, jev = run_trip(verra=True)
        self.assertTrue(any(b'"material":{' in sent(r) for r in jev.requests))
        c = by_key(trip["verdict"]["checks"])["material"]
        self.assertEqual((c["status"], c["detail"]), ("pass", "weighbill2.jpg: A material is entered"))
        # The first slip's material isn't checked.
        self.assertNotIn("material", by_key(trip["images"]["bill"][0]["checks"]))
        for answer, status, detail in ((("blank", 0.9), "fail", "weighbill2.jpg: No material entered"),
                                       (("empty_or_load", 0.9), "fail", "weighbill2.jpg: Material entered as EMPTY or LOAD, not a material"),
                                       (("no_field", 0.85), "review", "weighbill2.jpg: No material field on the slip"),
                                       (("filled", 0.6), "review", "weighbill2.jpg: Only 60% sure: a material is entered. Check the slip's material field.")):
            with self.subTest(answer=answer):
                v = run_trip(bill=(FIRST, {**FINAL, "material": answer}), verra=True)[0]["verdict"]
                self.assertEqual((by_key(v["checks"])["material"]["status"], by_key(v["checks"])["material"]["detail"]),
                                 (status, detail))
                self.assertEqual(v["status"], "problem" if status == "fail" else "review")
        v = run_trip(bill=(FIRST,), verra=True)[0]["verdict"]
        self.assertEqual(by_key(v["checks"])["material"]["status"], "review")

    def test_not_a_verra_project_trip_the_stamp_is_not_asked(self):
        trip, jev = run_trip()
        self.assertFalse(any(b"weighbridge_seal" in sent(r) or b"our_company" in sent(r) for r in jev.requests))
        self.assertNotIn("weighbridge_seal", by_key(trip["verdict"]["checks"]))

    def test_weighbridge_stamp_on_the_final_slip_of_a_verra_project_trip(self):
        trip, jev = run_trip(verra=True)
        v = trip["verdict"]
        self.assertEqual(v["status"], "pass", v["flags"])
        c = by_key(v["checks"])
        self.assertEqual(c["weighbridge_seal"]["detail"], "weighbill2.jpg: The weighbridge's own stamp is on it, with the heading's name")
        bills = [sent(r) for r in jev.requests if b"weigh_bill" in sent(r)]
        self.assertTrue(bills and all(b"weighbridge_seal" in b and proof_check.OUR_COMPANY.encode() in b for b in bills))
        self.assertFalse(any(b"weighbridge_seal" in sent(r) for r in jev.requests if b"weigh_bill" not in sent(r)))
        # The first slip isn't checked for stamps, so its unsure stamp answers don't count.
        self.assertNotIn("weighbridge_seal", by_key(trip["images"]["bill"][0]["checks"]))

    def test_a_missing_or_wrong_stamp_is_a_problem_and_an_unsure_one_asks_a_person(self):
        for picks, status in (({"weighbridge_seal": 0.05, "weighbridge_name": ("not_shown", 0.8)}, "fail"),
                              ({"weighbridge_name": ("different", 0.85)}, "fail"),
                              ({"weighbridge_seal": 0.55}, "review")):
            with self.subTest(picks=picks):
                v = run_trip(bill=(FIRST, {**FINAL, **picks}), verra=True)[0]["verdict"]
                self.assertEqual(by_key(v["checks"])["weighbridge_seal"]["status"], status)
                self.assertEqual(v["status"], "problem" if status == "fail" else "review")
        v = run_trip(bill=(FIRST,), verra=True)[0]["verdict"]
        self.assertEqual(by_key(v["checks"])["weighbridge_seal"]["status"], "review")

    def test_a_weighbridge_stamp_surely_there_with_an_unsure_name_asks_a_person(self):
        # As on 22 Oct 2026: a faint, upside-down stamp, surely there (0.88), its name unsure (0.46).
        bill = {**FINAL, "weighbridge_seal": 0.88, "weighbridge_name": ("same", 0.46)}
        c = by_key(run_trip(bill=(FIRST, bill), verra=True)[0]["verdict"]["checks"])["weighbridge_seal"]
        self.assertEqual((c["status"], c["detail"]),
                         ("review", "weighbill2.jpg: Only 46% sure: the weighbridge's own stamp is on it, with the heading's name. "
                                    "Compare the stamp's name with the slip's heading."))

    def test_unloading_at_the_plant_given(self):
        trip, jev = run_trip(unloading=({**UNLOADING, **AT_PLANT}, {**UNLOADING, **AT_PLANT}), plant=PLANT)
        v = trip["verdict"]
        self.assertEqual(v["status"], "pass", v["flags"])
        self.assertEqual(by_key(v["checks"])["unloading_plant"]["detail"],
                         "2 of 2 stamped within 500 m: unloading1.jpg at 11.4404, 75.9419, 0 m from the plant")
        position = next(x for x in trip["images"]["unloading"][0]["readings"] if x["key"] == "position")
        self.assertEqual((position["value"], position["coarse"]["value"]), ("11.4404, 75.9419", "11.44, 75.94"))
        # Only the unloading photos are asked where they were taken: the photo questions, then the line is found
        # (a Box question, on Jev 1.14) and the four-decimal values picked from candidates starting 11.44 and 75.94.
        bodies = [sent(r) for r in jev.requests]
        self.assertEqual(sum(b"lat_degrees" in b for b in bodies), 2)
        boxes = [b for b in bodies if b'"line":{"type":"box"' in b]
        self.assertEqual(len(boxes), 2)
        self.assertTrue(all(b'"jev-preview"' in b for b in boxes))
        picks = [b for b in bodies if b'"lat_pick":{' in b]
        self.assertEqual(len(picks), 2)
        self.assertTrue(all(b'"11.4400":null' in b and b'"11.4499":null' in b and b'"75.9400":null' in b for b in picks))
        self.assertEqual(trip["images"]["unloading"][0]["requests"], 3)

    def test_unloading_away_from_the_plant_is_a_problem(self):
        v = run_trip(unloading=({**UNLOADING, **AT_PLANT}, {**UNLOADING, **PULATH}), plant=PLANT)[0]["verdict"]
        self.assertEqual(v["status"], "problem")
        self.assertEqual(by_key(v["checks"])["unloading_plant"]["detail"],
                         "unloading2.jpg is stamped at 11.1649, 76.1549, 38 km from the plant, but unloading must be within 500 m of it.")
        c = by_key(run_trip(unloading=({**UNLOADING, **PULATH},), plant=PLANT, limits=Limits(plant_radius=50_000))[0]["verdict"]["checks"])
        self.assertEqual(c["unloading_plant"]["status"], "pass")

    def test_the_distance_is_measured_to_the_metre(self):
        # 600 m north: the same 0.01° square as the plant, so only the four-decimal pick shows it.
        north = {**UNLOADING, **stamped_at("11.445804", "75.942076")}
        c = by_key(run_trip(unloading=(north,), plant=PLANT)[0]["verdict"]["checks"])["unloading_plant"]
        self.assertEqual((c["status"], c["detail"]),
                         ("fail", "unloading1.jpg is stamped at 11.4458, 75.9420, 600 m from the plant, "
                                  "but unloading must be within 500 m of it."))
        c = by_key(run_trip(unloading=(north,), plant=PLANT, limits=Limits(plant_radius=700))[0]["verdict"]["checks"])
        self.assertEqual(c["unloading_plant"]["status"], "pass")
        # Picked unsurely, the two-decimal square can't tell 600 m from 0 m, so a person looks.
        unsure = {**UNLOADING, **stamped_at("11.445804", "75.942076", picked=0.6)}
        c = by_key(run_trip(unloading=(unsure,), plant=PLANT)[0]["verdict"]["checks"])["unloading_plant"]
        self.assertEqual((c["status"], c["detail"]), ("review", "The position on the GPS stamp of unloading1.jpg couldn't be read surely enough to tell if "
                                    "unloading was within 500 m of the plant. Check the Lat/Long line."))
        # And when the line can't be read, likewise; nothing is picked when the two decimals can't be read.
        unread = {**UNLOADING, **stamped_at("11.440404", "75.941976", picked=None)}
        self.assertEqual(by_key(run_trip(unloading=(unread,), plant=PLANT)[0]["verdict"]["checks"])["unloading_plant"]["status"], "review")
        trip, jev = run_trip(unloading=({**UNLOADING, **AT_PLANT, "lat_degrees": ("not_shown", 0.9)},), plant=PLANT)
        self.assertFalse(any(b'"line":{"type":"box"' in sent(r) for r in jev.requests))

    def test_a_torn_two_decimal_reading_against_a_sure_one_asks_a_person(self):
        # As 171535 on 19 Oct 2026: one photo read 11.40 (11.44 on the stamp) and its line not picked,
        # the other picked surely at the plant.
        torn = {**UNLOADING, **stamped_at("11.400404", "75.941976", confidence=0.97, picked=None)}
        c = by_key(run_trip(unloading=(torn, {**UNLOADING, **AT_PLANT}), plant=PLANT)[0]["verdict"]["checks"])["unloading_plant"]
        self.assertEqual(c["status"], "review")
        self.assertEqual(c["detail"], "The photos disagree: unloading1.jpg seems stamped at 11.40, 75.94, at least 3.4 km from "
                                      "the plant, but unloading2.jpg is at 11.4404, 75.9419, 0 m from the plant. Check both GPS stamps.")
        self.assertEqual(c["images"], ["unloading1.jpg", "unloading2.jpg"])

    def test_the_two_likeliest_starts_are_offered(self):
        torn = {**UNLOADING, **AT_PLANT, "lat_decimals": ("40", 0.5, {"44": 0.45})}
        _, jev = run_trip(unloading=(torn,), plant=PLANT)
        body = next(b for b in map(sent, jev.requests) if b'"lat_pick":{' in b)
        self.assertIn(b"starts with 11.40 or 11.44", body)
        self.assertTrue(b'"11.4000":null' in body and b'"11.4499":null' in body)
        self.assertIn(b"starts with 75.94.", body)  # the other options were each under 5% likely

    def test_one_clear_position_at_the_plant_is_enough(self):
        unread = {**UNLOADING, **AT_PLANT, "long_decimals": ("not_shown", 0.9)}
        c = by_key(run_trip(unloading=(unread, {**UNLOADING, **AT_PLANT}), plant=PLANT)[0]["verdict"]["checks"])
        self.assertEqual((c["unloading_plant"]["status"], c["unloading_plant"]["detail"]),
                         ("pass", "1 of 2 stamped within 500 m: unloading2.jpg at 11.4404, 75.9419, 0 m from the plant"))
        c = by_key(run_trip(unloading=(unread,), plant=PLANT)[0]["verdict"]["checks"])
        self.assertEqual((c["unloading_plant"]["status"], c["unloading_plant"]["detail"]),
                         ("review", "No position could be read on the GPS stamp of unloading1.jpg. Check the Lat/Long line "
                                    "places unloading within 500 m of the plant."))
        # Unsure digits that are far from the plant whichever way they're read are still a problem.
        either = {**UNLOADING, **stamped_at("11.164949", "76.154944", confidence=0.97, picked=None),
                  "lat_decimals": ("16", 0.55, {"11": 0.43})}
        c = by_key(run_trip(unloading=(either,), plant=PLANT)[0]["verdict"]["checks"])
        self.assertEqual(c["unloading_plant"]["status"], "fail")
        self.assertTrue(c["unloading_plant"]["detail"].startswith("unloading1.jpg is stamped away from the plant (9"), c["unloading_plant"])
        # And unsure between the plant's square and another 4 km off asks a person, as here.
        split = {**UNLOADING, **stamped_at("11.440404", "75.941976", picked=None), "lat_decimals": ("44", 0.55, {"40": 0.43})}
        c = by_key(run_trip(unloading=(split,), plant=PLANT)[0]["verdict"]["checks"])
        self.assertEqual(c["unloading_plant"]["status"], "review")
        # An unsure reading of a far place isn't a problem yet: a person looks.
        unsure = {**UNLOADING, **stamped_at("11.164949", "76.154944", confidence=0.5, picked=None)}
        c = by_key(run_trip(unloading=(unsure,), plant=PLANT)[0]["verdict"]["checks"])
        self.assertEqual((c["unloading_plant"]["status"], c["unloading_plant"]["detail"]),
                         ("review", "The position on the GPS stamp of unloading1.jpg couldn't be read surely enough to tell if "
                                    "unloading was within 500 m of the plant. Check the Lat/Long line."))

    def test_one_clear_matching_plate_per_kind_is_enough(self):
        hidden = {**LOADING, "plate": ("partly_readable", 0.9), "plate_number": ("cannot_tell", 0.9)}
        c = by_key(run_trip(loading=(hidden, LOADING))[0]["verdict"]["checks"])
        self.assertEqual((c["loading_plate"]["status"], c["loading_plate"]["detail"]), ("pass", "Seen clearly in 1 of 2"))
        c = by_key(run_trip(loading=(hidden, hidden))[0]["verdict"]["checks"])
        self.assertEqual(c["loading_plate"]["status"], "review")
        other = {**LOADING, "plate_number": ("different", 0.9)}
        c = by_key(run_trip(loading=(LOADING, other))[0]["verdict"]["checks"])
        self.assertEqual((c["loading_plate"]["status"], c["loading_plate"]["detail"]), ("fail", f"collection2.jpg shows a different number plate from {NUMBER}."))

    def test_every_photo_needs_a_vehicle_a_stamp_and_no_screen(self):
        c = by_key(run_trip(unloading=(UNLOADING, {**UNLOADING, "gps_stamp": 0.05}))[0]["verdict"]["checks"])
        self.assertEqual(c["unloading_stamp"]["detail"], "No GPS stamp on unloading2.jpg.")
        c = by_key(run_trip(unloading=({**UNLOADING, "screen_photo": 0.9}, UNLOADING))[0]["verdict"]["checks"])
        self.assertEqual((c["unloading_direct"]["status"], c["unloading_direct"]["detail"]), ("fail", "Photographed off a screen, not taken directly: unloading1.jpg."))
        c = by_key(run_trip(loading=(NOT_A_VEHICLE,))[0]["verdict"]["checks"])
        self.assertEqual(c["loading_vehicle"]["status"], "fail")

    def test_a_bill_photographed_off_a_screen_is_a_problem(self):
        # 172913 (23 Oct 2026): the slip shown on the weighbridge's computer, never printed.
        v = run_trip(bill=(FIRST, {**FINAL, "screen_photo": 0.95}))[0]["verdict"]
        c = by_key(v["checks"])["bill_direct"]
        self.assertEqual((v["status"], c["part"], c["status"], c["images"]), ("problem", "real", "fail", ["weighbill2.jpg"]))
        self.assertEqual(c["detail"], "Photographed off a screen, not taken directly: weighbill2.jpg.")
        c = by_key(run_trip(bill=(FIRST, {**FINAL, "screen_photo": 0.7}))[0]["verdict"]["checks"])["bill_direct"]
        self.assertEqual(c["status"], "review")
        c = by_key(run_trip()[0]["verdict"]["checks"])["bill_direct"]
        self.assertEqual((c["status"], c["detail"]), ("pass", "2 of 2 photos"))

    def test_every_slip_must_show_the_vehicle_number(self):
        v = run_trip(bill=({**FIRST, "bill_number": ("different", 0.9)}, FINAL))[0]["verdict"]
        self.assertEqual(by_key(v["checks"])["bill_number"]["detail"], f"weighbill1.jpg shows a different vehicle number from {NUMBER}.")
        v = run_trip(bill=({**FIRST, "bill_number": ("not_shown", 0.9)}, FINAL))[0]["verdict"]
        self.assertEqual(by_key(v["checks"])["bill_number"]["status"], "review")

    def test_swapped_gross_and_tare_labels_are_a_problem(self):
        v = run_trip(bill=(FIRST, {**FINAL, "gross_vs_tare": ("tare_heavier", 0.9)}))[0]["verdict"]
        self.assertEqual(by_key(v["checks"])["gross_vs_tare"]["detail"],
                         "weighbill2.jpg: Gross printed lighter than tare: the labels look swapped")
        # Unsure, or no labels to compare: no check at all, rather than asking a person.
        for answer in (("tare_heavier", 0.6), ("not_shown", 0.95)):
            v = run_trip(bill=(FIRST, {**FINAL, "gross_vs_tare": answer}))[0]["verdict"]
            self.assertNotIn("gross_vs_tare", by_key(v["checks"]))
            self.assertEqual(v["status"], "pass")

    def test_the_same_file_twice_is_asked_about_once(self):
        trip, jev = run_trip(bill=(FINAL, FINAL), same=[("bill", 0, "bill", 1)])
        self.assertEqual(len(jev.requests), 6)
        second = trip["images"]["bill"][1]
        self.assertEqual((second["same_as"], second["requests"], second["input_tokens"]), ("weighbill1.jpg", 0, 0))
        self.assertEqual(trip["verdict"]["status"], "pass")

    def test_missing_images_or_quantity_ask_a_person(self):
        v = run_trip(unloading=())[0]["verdict"]
        self.assertEqual((v["status"], by_key(v["checks"])["unloading_missing"]["detail"]), ("review", "No unloading photos added."))
        v = run_trip(quantity=None)[0]["verdict"]
        self.assertEqual(by_key(v["checks"])["net_weight"]["detail"], "No quantity was typed, so the net weight wasn't checked.")
        v = run_trip(bill=())[0]["verdict"]
        self.assertEqual(by_key(v["checks"])["bill_missing"]["status"], "review")

    def test_limits_can_change_for_each_run(self):
        v = run_trip(limits=Limits(min_gap_tare_first=120))[0]["verdict"]
        self.assertEqual(by_key(v["checks"])["weigh_gap"]["detail"], "The tare (empty) and gross (loaded) weighings are only 1 h 30 min apart, "
                         "but at least 2 h is needed for loading between them.")

    def test_trip_past_midnight(self):
        bill = (FIRST, {**FINAL, **at("tare", "23:30", "14/10"), **at("gross", "01:00", "15/10")})
        v = run_trip(loading=({**LOADING, **stamp("00:10")},), unloading=({**UNLOADING, **stamp("01:20")},), bill=bill)[0]["verdict"]
        self.assertEqual(v["status"], "pass")
        self.assertEqual(by_key(v["checks"])["loading_timing"]["detail"],
                         "Loading 00:10 on 15/10 is between tare 23:30 on 14/10 and gross 01:00 on 15/10")
        self.assertEqual(by_key(v["checks"])["weigh_day"]["detail"], "Across midnight, 1 h 30 min apart (at most 12 h)")

    def test_am_and_pm_on_a_bill_become_a_24_hour_time(self):
        # As on the G W slips: tare 12:27:48 PM, gross 03:06:08 PM.
        bill = (FIRST, {**FINAL, **at("tare", "12:27", ampm="pm"), **at("gross", "03:06", ampm="pm")})
        v = run_trip(loading=({**LOADING, **stamp("14:13")},), unloading=({**UNLOADING, **stamp("15:14")},), bill=bill)[0]["verdict"]
        self.assertEqual(v["status"], "pass")
        self.assertEqual(by_key(v["checks"])["weigh_order"]["detail"],
                         "Tare first (12:27), then gross (15:06), from the times printed with each weight")
        self.assertEqual([proof_check.to_24h(t, a) for t, a in [("03:06", "pm"), ("12:30", "am"), ("12:30", "pm"), ("15:06", "pm"), ("07:39", "none")]],
                         ["15:06", "00:30", "12:30", "15:06", "07:39"])

    def test_hour_as_printed_and_on_a_24_hour_clock_count_together(self):
        # As on 173284: DateTime Out 02:45:57 PM, its hour read 02 at 0.52 and 14 at 0.46.
        bill = {**IN_OUT_FINAL, **at("in", "08:49", ampm="am"), **at("out", "02:45", ampm="pm"),
                "out_hour": ("02", 0.52, {"14": 0.46})}
        v = run_trip(loading=({**LOADING, **stamp("13:36")},), unloading=({**UNLOADING, **stamp("18:37")},), bill=(bill,))[0]["verdict"]
        self.assertEqual(by_key(v["checks"])["weigh_order"]["status"], "pass")
        self.assertEqual(by_key(v["checks"])["weigh_order"]["detail"],
                         "Tare first (08:49), then gross (14:45), from Time In and Time Out, tare first")
        # With AM, 02 and 14 are different times, so the hour stays unsure.
        hour = {"choice": "02", "confidence": 0.52, "top": [("02", 0.54), ("14", 0.46)]}
        self.assertEqual(proof_check.same_hour(hour, "am")["confidence"], 0.52)
        self.assertAlmostEqual(proof_check.same_hour(hour, "pm")["confidence"], 0.98)

    def test_photo_of_a_screen_has_its_own_measured_thresholds(self):
        for yes, status in [(0.43, "pass"), (0.7, "review"), (0.99, "fail")]:
            with self.subTest(yes=yes):
                c = by_key(run_trip(unloading=({**UNLOADING, "screen_photo": yes},))[0]["verdict"]["checks"])
                self.assertEqual(c["unloading_direct"]["status"], status)

    def test_time_in_and_out_layout(self):
        v = run_trip(bill=(IN_OUT_FINAL,))[0]["verdict"]
        self.assertEqual(v["status"], "pass")
        self.assertEqual(by_key(v["checks"])["weigh_order"]["detail"],
                         "Tare first (02:00), then gross (03:30), from Time In and Time Out, tare first")
        self.assertEqual(by_key(v["checks"])["time_in_tare"]["status"], "pass")

    def test_times_printed_at_the_top_of_the_first_and_final_slips(self):
        v = run_trip(bill=(LAKSHMI_FIRST, LAKSHMI_FINAL))[0]["verdict"]
        self.assertEqual(v["status"], "pass", v["flags"])
        self.assertEqual(by_key(v["checks"])["weigh_order"]["detail"], "Tare first (02:00), then gross (03:30), from the times "
                                                                       "printed at the top of the first and final slips, tare first")
        # Without the first slip there's no first weighing time.
        v = run_trip(bill=(LAKSHMI_FINAL,))[0]["verdict"]
        self.assertEqual(by_key(v["checks"])["weigh_order"]["detail"],
                         "The tare (empty) and gross (loaded) times on weighbill1.jpg couldn't be read, so the weighing "
                         "order and photo times weren't checked. Read the times on the slip.")
        # The first weight heavier: weighed loaded first, so the first slip's time is the gross.
        v = run_trip(bill=(LAKSHMI_FIRST, {**LAKSHMI_FINAL, "first_weight": ("gross", 0.9)}))[0]["verdict"]
        self.assertTrue(by_key(v["checks"])["weigh_order"]["detail"].startswith("Gross first (02:00), then tare (03:30)"))

    def test_the_heavier_weight_at_time_in_is_flagged(self):
        # Details entered swapped, or weighed loaded first: a problem a person looks at.
        v = run_trip(bill=({**IN_OUT_FINAL, "first_weight": ("gross", 0.9)},))[0]["verdict"]
        c = by_key(v["checks"])["time_in_tare"]
        self.assertEqual((c["status"], c["detail"]),
                         ("fail", "weighbill1.jpg: the heavier weight is at Time In (02:00). Either In and Out were entered "
                                  "swapped, or the vehicle was weighed loaded first."))
        self.assertEqual(v["status"], "problem")
        c = by_key(run_trip(bill=({**IN_OUT_FINAL, "first_weight": ("gross", 0.6)},))[0]["verdict"]["checks"])["time_in_tare"]
        self.assertEqual(c["status"], "review")
        # Slips with a time beside each weight aren't In/Out slips, so this isn't checked.
        self.assertNotIn("time_in_tare", by_key(run_trip()[0]["verdict"]["checks"]))

    def test_first_and_second_weights_with_both_times_labelled_gross(self):
        # As Jev read a Kuttiyadi slip on 22 Oct 2026: both times labelled Gross, so the times with each weight
        # were unsure (gross 0.61), but read surely as first and second, and the first weight is the lighter.
        bill = {**FINAL, **at("gross", "03:30", confidence=0.61), **at("tare", "02:00", confidence=0.8),
                **at("in", "02:00", confidence=1.0), **at("out", "03:30", confidence=1.0), "first_weight": ("tare", 0.95)}
        v = run_trip(bill=(bill,))[0]["verdict"]
        self.assertEqual(v["status"], "pass", v["flags"])
        self.assertEqual(by_key(v["checks"])["weigh_order"]["detail"],
                         "Tare first (02:00), then gross (03:30), from Time In and Time Out, tare first")

    def test_nothing_is_rejected_and_problems_come_first(self):
        self.assertNotIn("fail", proof_check.TITLES)
        v = run_trip(loading=({**LOADING, "shows_vehicle": 0.6, "plate_number": ("different", 0.9)},))[0]["verdict"]
        self.assertEqual([c["status"] for c in v["flags"]], ["fail", "review"])
        self.assertEqual([c["key"] for c in v["flags"]], ["loading_plate", "loading_vehicle"])


class WeighbridgeTests(unittest.TestCase):
    """The final slip is from the weighbridge entered (the verifier sends gw_sales' claims.weighbridge.name)."""

    NAME = "S N Automatic Weigh Bridge"

    def test_asked_only_when_a_weighbridge_is_entered(self):
        _, jev = run_trip()
        self.assertFalse(any(b"weighbridge_entered" in sent(r) for r in jev.requests))
        _, jev = run_trip(weighbridge=self.NAME)
        bills = [sent(r) for r in jev.requests if b"weigh_bill" in sent(r)]
        self.assertTrue(bills and all(b"weighbridge_entered" in b and self.NAME.encode() in b for b in bills))
        photos = [sent(r) for r in jev.requests if b"shows_vehicle" in sent(r)]
        self.assertFalse(any(b"weighbridge_entered" in b for b in photos), "only bills are asked")

    def test_saved_answers_keep_their_keys_without_a_weighbridge(self):
        prints = {"md5": "abc"}
        self.assertEqual(proof_check.question_version("bill"), proof_check.question_version("bill", weighbridge=False))
        self.assertNotEqual(proof_check.question_version("bill"), proof_check.question_version("bill", weighbridge=True))
        plain = proof_check.answer_key(prints, "bill", NUMBER, QUANTITY, "jev-latest")
        self.assertEqual(len(plain.split("|")), 7, "as before the question was added")
        named = proof_check.answer_key(prints, "bill", NUMBER, QUANTITY, "jev-latest", weighbridge=self.NAME)
        self.assertTrue(named.endswith("|" + self.NAME))
        self.assertNotEqual(plain.split("|")[2], named.split("|")[2])

    def outcome(self, answer):
        trip, _ = run_trip(weighbridge=self.NAME, bill=(FIRST, {**FINAL, "weighbridge_entered": answer}))
        return by_key(trip["verdict"]["checks"])["weighbridge_matches"]

    def test_same_passes_different_is_a_problem_unsure_asks(self):
        same = self.outcome(("same", 0.9))
        self.assertEqual((same["status"], same["part"], same["detail"]), ("pass", "docs", f"weighbill2.jpg is from {self.NAME}"))
        different = self.outcome(("different", 0.9))
        self.assertEqual(different["status"], "fail")
        self.assertIn(f"from another weighbridge than {self.NAME}, the one entered", different["detail"])
        unsure = self.outcome(("same", 0.6))
        self.assertEqual(unsure["status"], "review")
        self.assertEqual(unsure["detail"], f"Only 60% sure: weighbill2.jpg is from {self.NAME}. Compare the slip's heading "
                                           f"with {self.NAME}.")
        self.assertEqual(self.outcome(("not_shown", 0.9))["status"], "review")

    def test_no_bills_asks_a_person_with_the_missing_bills(self):
        trip, _ = run_trip(weighbridge=self.NAME, bill=())
        c = by_key(trip["verdict"]["checks"])["weighbridge_matches"]
        self.assertEqual((c["status"], c["cause"]), ("review", "bill_missing"))


class PartTests(unittest.TestCase):
    def test_every_check_is_in_a_part(self):
        self.assertEqual(set(proof_check.PART_OF.values()), set(proof_check.PARTS))
        v = run_trip(verra=True, unloading=({**UNLOADING, **AT_PLANT},), plant=PLANT,
                     bill=(FIRST, IN_OUT_FINAL), index=proof_check.ImageIndex())[0]["verdict"]
        self.assertEqual(v["status"], "pass", v["flags"])
        self.assertEqual({p: [c["key"] for c in v["parts"][p]["checks"]] for p in v["parts"]}, {
            "real": ["loading_direct", "loading_plate", "unloading_direct", "unloading_plate", "unloading_plant", "bill_number",
                     "bill_direct", "weigh_order", "time_in_tare", "weigh_gap", "weigh_day", "loading_timing", "unloading_timing", "unloading_day", "reused"],
            "docs": ["loading_vehicle", "loading_stamp", "unloading_vehicle", "unloading_stamp", "bill_found", "net_weight",
                     "gross_vs_tare"],
            "verra": ["material", "weighbridge_seal"],
        })

    def test_each_part_is_decided_on_its_own(self):
        v = run_trip()[0]["verdict"]
        self.assertEqual({p: x["status"] for p, x in v["parts"].items()}, {"real": "pass", "docs": "pass"})
        # A photo with no GPS stamp is a gap in the proof, not a sign the trip didn't happen.
        v = run_trip(unloading=(UNLOADING, {**UNLOADING, "gps_stamp": 0.05}))[0]["verdict"]
        self.assertEqual({p: x["status"] for p, x in v["parts"].items()}, {"real": "pass", "docs": "problem"})
        self.assertEqual((v["status"], v["title"]), ("problem", "Flagged: problem found"))
        self.assertEqual([c["key"] for c in v["parts"]["docs"]["flags"]], ["unloading_stamp"])
        # Another vehicle's plate is: the trip is the worst of its parts.
        v = run_trip(loading=({**LOADING, "plate_number": ("different", 0.9)},), bill=(FIRST, {**FINAL, "net_weight": ("same", 0.6)}),
                     verra=True)[0]["verdict"]
        self.assertEqual({p: x["status"] for p, x in v["parts"].items()}, {"real": "problem", "docs": "review", "verra": "pass"})
        self.assertEqual(v["status"], "problem")

    def test_missing_images_ask_a_person_in_the_real_part_too(self):
        v = run_trip(loading=())[0]["verdict"]
        self.assertEqual([c["key"] for c in v["parts"]["docs"]["flags"]], ["loading_missing"])
        self.assertEqual([(c["key"], c["detail"]) for c in v["parts"]["real"]["flags"]], [("loading_plate", f"No loading photos to compare with {NUMBER}.")])
        v = run_trip(bill=(), verra=True)[0]["verdict"]
        self.assertEqual({p: x["status"] for p, x in v["parts"].items()}, {"real": "review", "docs": "review", "verra": "review"})
        self.assertEqual([(c["key"], c["detail"]) for c in v["parts"]["real"]["flags"]],
                         [("bill_number", f"No weigh bills to compare with {NUMBER}."),
                          ("weigh_order", "No weigh bills, so the weighing order and photo times weren't checked.")])
        self.assertEqual({c["cause"] for c in v["parts"]["real"]["flags"]}, {"bill_missing"})

    def test_checks_across_trips_are_in_the_real_part(self):
        uploads, _ = make_trip()
        index = proof_check.ImageIndex()
        index.add("day/171562", "collection2.jpg", proof_check.fingerprints(uploads["loading"][0].data))
        trip = run_trip(index=index, trip_id="day/171496")[0]
        self.assertEqual({p: x["status"] for p, x in trip["verdict"]["parts"].items()}, {"real": "problem", "docs": "pass"})
        trip = run_trip()[0]
        proof_check.add_checks(trip, [proof_check.check("trips", "overlap", "One trip at a time", "fail", "Also on trip 171566")])
        self.assertEqual({p: x["status"] for p, x in trip["verdict"]["parts"].items()}, {"real": "problem", "docs": "pass"})
        self.assertEqual(trip["verdict"]["status"], "problem")


class CrossTripTests(unittest.TestCase):
    def test_an_image_used_in_another_trip_is_a_problem(self):
        uploads, _ = make_trip()
        index = proof_check.ImageIndex()
        index.add("day/171562", "collection2.jpg", proof_check.fingerprints(uploads["loading"][0].data))
        v = run_trip(index=index, trip_id="day/171496")[0]["verdict"]
        self.assertEqual(v["status"], "problem")
        self.assertEqual(by_key(v["checks"])["reused"]["detail"], "collection1.jpg is also in trip 171562. The same image can't be proof for two trips.")
        self.assertEqual(by_key(run_trip(index=index, trip_id="day/171562")[0]["verdict"]["checks"])["reused"]["status"], "pass")
        index.add("day/171566", "unloading1.jpg", proof_check.fingerprints(uploads["unloading"][0].data))
        index.add("day/171531", "unloading1.jpg", proof_check.fingerprints(uploads["unloading"][0].data))
        index.add("day/171562", "collection1.jpg", proof_check.fingerprints(uploads["loading"][1].data))
        v = run_trip(index=index, trip_id="day/171496")[0]["verdict"]
        self.assertEqual(by_key(v["checks"])["reused"]["detail"],
                         "collection1.jpg and collection2.jpg are also in trip 171562; unloading1.jpg is also in trips 171531 and 171566. "
                         "The same image can't be proof for two trips.")

    def test_a_resaved_copy_looks_alike_and_asks_a_person(self):
        index = proof_check.ImageIndex()
        index.add("day/171562", "collection2.jpg", proof_check.fingerprints(coloured(COLOURS[0], quality=60)))
        trip, _ = run_trip(index=index, trip_id="day/171496")
        self.assertEqual(by_key(trip["verdict"]["checks"])["reused"]["status"], "review")
        self.assertEqual(trip["images"]["loading"][0]["used_in"], [{"trip": "day/171562", "name": "collection2.jpg", "exact": False}])

    def test_index_of_trip_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp, "day", "171773")
            folder.mkdir(parents=True)
            (folder / "collection1.jpg").write_bytes(coloured(COLOURS[0]))
            (folder / "weighbill1.heic").write_bytes(coloured(COLOURS[1]))
            (folder / "notes.txt").write_text("not an image")
            index = proof_check.ImageIndex.of_folders(Path(tmp))
            self.assertEqual(sorted((t, n) for t, n, _, _ in index.items), [("day/171773", "collection1.jpg"), ("day/171773", "weighbill1.heic")])
            uploads = proof_check.read_trip_folder(folder)
            self.assertEqual({r: [u.name for u in us] for r, us in uploads.items()},
                             {"loading": ["collection1.jpg"], "unloading": [], "bill": ["weighbill1.heic"]})

    def test_a_vehicle_on_two_trips_at_once(self):
        def trip(number, start, end):
            return {"vehicle_number": number, "verdict": {"window": [start, end], "checks": [], "flags": []}}
        trips = {"171531": trip(NUMBER, "2024-10-19T08:32", "2024-10-19T10:10"),
                 "171566": trip(NUMBER, "2024-10-19T08:32", "2024-10-19T10:10"),
                 "171562": trip(NUMBER, "2024-10-19T08:13", "2024-10-19T08:29"),
                 "171773": trip("KL57AB8815", "2024-10-19T08:00", "2024-10-19T11:00")}
        found = proof_check.overlap_checks(trips)
        self.assertEqual(set(found), {"171531", "171566"})
        self.assertEqual(found["171531"]["detail"], f"{NUMBER} is also on trip 171566 at the same time, by the photos' stamps. "
                                                    "A vehicle can't be on two trips at once.")
        proof_check.add_checks(trips["171531"], [found["171531"]])
        self.assertEqual(trips["171531"]["verdict"]["status"], "problem")

    def test_trips_touching_within_the_clock_slack_are_not_on_two_trips(self):
        def trip(start, end):
            return {"vehicle_number": NUMBER, "verdict": {"window": [start, end], "checks": [], "flags": []}}
        trips = {"1": trip("2024-10-19T07:52", "2024-10-19T09:35"), "2": trip("2024-10-19T09:31", "2024-10-19T11:00")}
        self.assertEqual(proof_check.overlap_checks(trips), {}, "4 min apart: within the 5 min clock slack")
        self.assertEqual(set(proof_check.overlap_checks(trips, slack=3)), {"1", "2"})

    def test_a_morning_tare_on_every_slip_is_no_window(self):
        # 19 Oct 2026, KL57AD9265: weighed empty at 05:10, that tare printed on slips with gross 07:02 and 09:23.
        # By the weighings both trips would run from 05:10 and overlap; by the photos they don't.
        first, _ = run_trip(bill=(FIRST, {**FINAL, **at("tare", "01:00")}))
        later = {**PHOTO, **stamp("06:40")}
        second, _ = run_trip(loading=({**PHOTO, **stamp("05:20")},) * 2, unloading=(later, later),
                             bill=(FIRST, {**FINAL, **at("tare", "01:00"), **at("gross", "06:30")}))
        trips = {n: {"vehicle_number": NUMBER, "verdict": t["verdict"]} for n, t in (("1", first), ("2", second))}
        self.assertEqual(proof_check.overlap_checks(trips), {})


class SavedAnswerTests(unittest.TestCase):
    def test_saved_answers_are_reused_without_asking(self):
        cache = {}
        first, jev = run_trip(cache=cache)
        self.assertEqual((len(jev.requests), len(cache)), (8, 6))  # each bill's slip is found, then asked about
        uploads, _ = make_trip()
        again, _ = proof_check.analyze_trip(None, uploads, NUMBER, "jev-latest", QUANTITY, Limits(), cache=cache)
        self.assertEqual(again["verdict"]["checks"], first["verdict"]["checks"])
        # A different quantity changes what the bill was asked, so its saved answers don't apply.
        prints = proof_check.fingerprints(uploads["bill"][0].data)
        keys = {proof_check.answer_key(prints, "bill", NUMBER, q, "jev-latest") for q in ("2570", "840")}
        self.assertEqual(len(keys), 2)

    def test_answers_saved_for_another_company_name_are_not_reused(self):
        before, plain = proof_check.question_version("bill", verra=True), proof_check.question_version("bill")
        with mock.patch.object(proof_check.questions, "OUR_COMPANY", "Another Company"):
            self.assertNotEqual(proof_check.question_version("bill", verra=True), before)
            self.assertEqual(proof_check.question_version("bill"), plain)

    def test_reading_the_position_is_saved_apart_and_serves_any_plant(self):
        cache = {}
        unloading = ({**UNLOADING, **AT_PLANT}, {**UNLOADING, **AT_PLANT})
        run_trip(cache=cache)
        _, jev = run_trip(cache=cache, unloading=unloading, plant=PLANT)
        self.assertEqual(len(jev.requests), 6)  # only the unloading photos, asked again with the place questions, found and picked
        uploads, _ = make_trip(unloading=unloading)
        far, _ = proof_check.analyze_trip(None, uploads, NUMBER, "jev-latest", QUANTITY, cache=cache, plant=(11.1649, 76.1549))
        self.assertEqual(by_key(far["verdict"]["checks"])["unloading_plant"]["status"], "fail")


class QuestionTests(unittest.TestCase):
    def test_time_and_date_questions_state_their_premise_and_allow_not_shown(self):
        premises = {"stamp": proof_check.STAMP, "gross": proof_check.GROSS, "tare": proof_check.TARE,
                    "in": proof_check.TIME_IN, "out": proof_check.TIME_OUT, "printed": proof_check.PRINTED}
        for slot in proof_check.SLOTS:
            for qid in [q for _, _, qids, _ in proof_check.REPORT[slot] for q in qids]:
                question = proof_check.QUESTIONS[slot][qid]
                with self.subTest(qid=qid):
                    self.assertTrue(question.instructions.startswith(premises[qid.split("_")[0]]))
                    self.assertIn(proof_check.NOT_SHOWN, question.criteria)

    def test_number_questions_compare_with_the_typed_value_and_never_lead_with_same(self):
        for slot, qid, typed in (("photo", "plate_number", "vehicle_number"), ("bill", "bill_number", "vehicle_number"),
                                 ("bill", "net_weight", "quantity_kg")):
            question = proof_check.QUESTIONS[slot][qid]
            with self.subTest(qid=qid):
                self.assertIn(f"`{typed}`", question.instructions)
                self.assertNotEqual(next(iter(question.criteria)), "same")

    def test_every_check_has_an_outcome_for_every_option(self):
        for qid, (_, outcomes) in proof_check.CHOICE_CHECKS.items():
            slot = "bill" if qid in proof_check.QUESTIONS["bill"] else "photo"
            self.assertEqual(set(outcomes), set(proof_check.QUESTIONS[slot][qid].criteria), qid)
        for qid in proof_check.NOUL_CHECKS:
            self.assertTrue(any(qid in qs for qs in proof_check.QUESTIONS.values()), qid)

    def test_place_questions_state_their_premise_and_allow_not_shown(self):
        for qid in proof_check.PLACE:
            question = proof_check.QUESTIONS["photo"][qid]
            with self.subTest(qid=qid):
                self.assertTrue(question.instructions.startswith(proof_check.STAMP))
                self.assertIn(proof_check.NOT_SHOWN, question.criteria)
                self.assertLessEqual(len(question.criteria), 255)
        self.assertEqual(set(proof_check.questions_for("photo", None)) | set(proof_check.PLACE),
                         set(proof_check.questions_for("photo", None, place=True)))
        self.assertNotEqual(proof_check.question_version("photo"), proof_check.question_version("photo", place=True))

    def test_verra_questions_only_for_a_verra_project_trip(self):
        self.assertEqual(set(proof_check.VERRA), {"material", *proof_check.SEAL})
        self.assertEqual(set(proof_check.questions_for("bill", QUANTITY)) | set(proof_check.VERRA),
                         set(proof_check.questions_for("bill", QUANTITY, verra=True)))
        self.assertFalse(set(proof_check.VERRA) & set(proof_check.questions_for("bill", QUANTITY)))
        self.assertNotEqual(proof_check.question_version("bill"), proof_check.question_version("bill", verra=True))
        question = proof_check.QUESTIONS["bill"]["weighbridge_name"]
        self.assertNotEqual(next(iter(question.criteria)), "same")
        for qid in proof_check.SEAL:
            self.assertIn("`our_company`", proof_check.QUESTIONS["bill"][qid].instructions)

    def test_question_version_changes_with_the_questions(self):
        before = proof_check.question_version("photo")
        with mock.patch.dict(proof_check.QUESTIONS["photo"], {"extra": Noul(instructions="Is `photo` blue?")}):
            self.assertNotEqual(proof_check.question_version("photo"), before)


class MeasureAccuracyTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(measure_accuracy.normalize("photo_time", "4:09"), "04:09")
        self.assertEqual(measure_accuracy.normalize("photo_date", "15/10/2026"), "15/10")
        self.assertEqual(measure_accuracy.normalize("gross_date", "5.1.2026"), "05/01")
        self.assertEqual(measure_accuracy.normalize("in_time", "7:39"), "07:39")
        self.assertEqual(measure_accuracy.normalize("photo_time", " Not_Shown "), "not_shown")
        self.assertEqual(measure_accuracy.normalize("gps_stamp", "Yes"), "yes")

    def test_scores_and_reports_example_labels(self):
        rows = measure_accuracy.load_labels(ROOT / "examples" / "labels.example.csv")
        rows[2]["in_date"] = "16/10/2026"  # a deliberately wrong label, to see a mistake reported
        wrong_number_photo = {**PHOTO, "plate_number": ("different", 0.85)}
        # The sample slip prints only Time In and Time Out.
        sample_bill = {**IN_OUT_FINAL, **at("in", "03:09"), **at("out", "08:57")}
        wrong_number_bill = {**FINAL, "bill_number": ("different", 0.8)}
        wrong_weight_bill = {**FINAL, "net_weight": ("different", 0.85)}
        clock = {**NOT_A_VEHICLE, "stamp_hour": ("not_shown", 0.9), "stamp_minute": ("not_shown", 0.9),
                 "stamp_day": ("not_shown", 0.9), "stamp_month": ("not_shown", 0.9)}
        jev = FakeJev(photo=[PHOTO, wrong_number_photo, clock], bill=[sample_bill, wrong_number_bill, wrong_weight_bill])
        with jev.client() as client:
            results = measure_accuracy.run(client, rows, "jev-latest")
        items = measure_accuracy.score(rows, results)

        by_field = {(i["row"], i["field"]): i for i in items}
        self.assertEqual(len(items), 22)  # 6 + 1 photo, 7 + 1 + 1 bill, 6 for the clock
        self.assertTrue(by_field[("images/photo.png | photo | KL05AF9341", "plate_number")]["correct"])
        self.assertTrue(by_field[("images/photo.png | photo | KL05AF9314", "plate_number")]["correct"])
        self.assertTrue(by_field[("images/bill.png | bill | KL05AF9314 | 2570 kg", "bill_number")]["correct"])
        self.assertTrue(by_field[("images/bill.png | bill | KL05AF9341 | 2570 kg", "net_weight")]["correct"])
        self.assertTrue(by_field[("images/bill.png | bill | KL05AF9341 | 2750 kg", "net_weight")]["correct"])
        self.assertTrue(by_field[("images/bill.png | bill | KL05AF9341 | 2570 kg", "out_time")]["correct"])
        self.assertFalse(by_field[("images/bill.png | bill | KL05AF9341 | 2570 kg", "in_date")]["correct"])
        clock_vehicle = by_field[("../experiments/clock_reading/samples/analog-1042.jpg | photo | KL05AF9341", "shows_vehicle")]
        self.assertEqual((clock_vehicle["got"], clock_vehicle["confidence"]), ("no", 0.97))

        text = measure_accuracy.report(items, results, rows, "jev-latest")
        self.assertIn("Mistakes (1)", text)
        self.assertIn("16/10", text)
        self.assertIn("vehicle number: what each threshold would do", text)
        self.assertIn("net weight: what each threshold would do", text)

    def test_rejects_a_field_the_slot_does_not_check(self):
        bad = Path(self.id() + ".csv")
        try:
            bad.write_text("file,slot,vehicle_number,plate\nsamples/bill.png,bill,KL05AF9341,readable\n")
            with self.assertRaises(SystemExit) as caught:
                measure_accuracy.load_labels(bad)
            self.assertIn("isn't checked on a bill", str(caught.exception))
        finally:
            bad.unlink()

    def test_net_weight_label_needs_a_quantity(self):
        bad = Path(self.id() + ".csv")
        try:
            bad.write_text("file,slot,vehicle_number,quantity,net_weight\nsamples/bill.png,bill,KL05AF9341,,same\n")
            with self.assertRaises(SystemExit) as caught:
                measure_accuracy.load_labels(bad)
            self.assertIn("net_weight needs the quantity", str(caught.exception))
        finally:
            bad.unlink()


TRUTH = {"trip": "171773", "vehicle_number": NUMBER, "case": "tare_first", "net_kg": 2570,
         "photos": {"collection1.jpg": {"plate": NUMBER, "stamp_at": "15/10/2026 02:40"},
                    "collection2.jpg": {"plate": "unreadable", "stamp_at": "15/10/2026 02:41"},
                    "unloading1.jpg": {"plate": NUMBER, "stamp_at": "15/10/2026 04:09"},
                    "unloading2.jpg": {"plate": NUMBER, "stamp_at": "15/10/2026 04:09"}},
         "bills": {"weighbill1.jpg": {"slip": "first", "vehicle_number": NUMBER, "tare_at": "15/10/2026 02:00"},
                   "weighbill2.jpg": {"slip": "final", "vehicle_number": NUMBER, "tare_at": "15/10/2026 02:00",
                                      "gross_at": "15/10/2026 03:30"}}}


class CheckTripFoldersTests(unittest.TestCase):
    def test_typed_values_from_trip_json_or_hand_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp, "171773")
            folder.mkdir()
            (folder / "collection1.jpg").write_bytes(coloured(COLOURS[0]))
            (Path(tmp) / "truth.jsonl").write_text(json.dumps(TRUTH) + "\n")
            truth = check_trip_folders.find_truth(folder)
            self.assertEqual(check_trip_folders.typed_for(folder, truth.get("171773")),
                             ({"vehicle_number": NUMBER, "quantity": 2570}, "hand-read"))
            (folder / "trip.json").write_text(json.dumps({"vehicle_number": "KL05AF9314", "quantity": 840}))
            self.assertEqual(check_trip_folders.typed_for(folder, truth.get("171773"))[1], "trip.json")
            self.assertEqual(check_trip_folders.find_trips(Path(tmp)), [folder])

    def test_compare_with_hand_read_values(self):
        trip, _ = run_trip()
        items = check_trip_folders.compare(trip, TRUTH, {"vehicle_number": NUMBER})
        wrong = {(i["field"], i["image"]) for i in items if not i["right"]}
        # collection2 was hand-read as 02:41 and its plate as unreadable; the fake Jev read 02:40 and "same".
        self.assertEqual(wrong, {("photo time", "collection2.jpg"), ("plate matches", "collection2.jpg")})
        fields = {i["field"] for i in items}
        self.assertEqual(fields, {"photo time", "plate matches", "slip kind", "bill number", "tare time", "gross time", "weighing order"})


class CheckTripFoldersPlaceTests(unittest.TestCase):
    def test_compare_positions_with_hand_read_values(self):
        unloading = ({**UNLOADING, **AT_PLANT}, {**UNLOADING, **stamped_at("11.440504", "75.941976")})
        trip, _ = run_trip(unloading=unloading, plant=PLANT)
        truth = json.loads(json.dumps(TRUTH))
        for name in ("unloading1.jpg", "unloading2.jpg"):
            truth["photos"][name] |= {"lat": 11.440404, "long": 75.941976}
        items = [i for i in check_trip_folders.compare(trip, truth, {"vehicle_number": NUMBER}) if i["field"] == "photo position"]
        self.assertEqual([(i["image"], i["expected"], i["right"]) for i in items],
                         [("unloading1.jpg", "11.4404, 75.9419", True), ("unloading2.jpg", "11.4404, 75.9419", False)])


class TestPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.jev = FakeJev()
        cls.real_client = test_page_server.TypeSafeClient
        test_page_server.TypeSafeClient = lambda **kw: cls.real_client(**{**kw, "api_key": "test"}, transport=httpx2.MockTransport(cls.jev))
        cls.old_key = os.environ.get("TYPESAFE_API_KEY")
        os.environ["TYPESAFE_API_KEY"] = "test"
        cls.tmp = tempfile.TemporaryDirectory()
        folder = Path(cls.tmp.name, "day", "171773")
        folder.mkdir(parents=True)
        for name, colour in (("collection1.jpg", COLOURS[0]), ("unloading1.jpg", COLOURS[1]), ("weighbill1.heic", COLOURS[2])):
            (folder / name).write_bytes(coloured(colour))
        Path(cls.tmp.name, "day", "truth.jsonl").write_text(json.dumps(TRUTH) + "\n")
        cls.trips = mock.patch.object(test_page_server, "TRIPS", Path(cls.tmp.name))
        cls.trips.start()
        test_page_server._index = None
        cls.server = test_page_server.serve(0)  # any free port
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        test_page_server.TypeSafeClient = cls.real_client
        cls.trips.stop()
        test_page_server._index = None
        cls.tmp.cleanup()
        if cls.old_key is None:
            os.environ.pop("TYPESAFE_API_KEY", None)
        else:
            os.environ["TYPESAFE_API_KEY"] = cls.old_key

    def post(self, body: dict) -> tuple[int, dict]:
        request = urllib.request.Request(self.base + "/api/check", json.dumps(body).encode(), {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.load(error)

    def get(self, path: str) -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(self.base + path) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.load(error)

    @staticmethod
    def image(colour) -> dict:
        return {"name": "x.jpg", "data": "data:image/jpeg;base64," + base64.b64encode(coloured(colour)).decode()}

    def test_page_and_status(self):
        with urllib.request.urlopen(self.base + "/") as response:
            self.assertIn(b"Proof Check Bench", response.read())
        status, body = self.get("/api/status")
        self.assertTrue(body["key_set"])
        self.assertEqual(body["limits"], {"min_gap_tare_first": 60, "min_gap_gross_first": 10, "clock_slack": 5, "max_overnight_gap": 720,
                                          "plant_radius": 500})

    def test_lists_and_loads_trip_folders(self):
        status, trips = self.get("/api/trips")
        self.assertEqual(trips, [{"id": "day/171773", "name": "171773", "counts": {"loading": 1, "unloading": 1, "bill": 1},
                                  "vehicle_number": NUMBER, "typed_from": "hand-read"}])
        status, trip = self.get("/api/trips/day/171773")
        self.assertEqual(status, 200)
        self.assertEqual(trip["typed"], {"vehicle_number": NUMBER, "quantity": 2570})
        self.assertTrue(trip["images"]["bill"][0]["data"].startswith("data:image/jpeg;base64,"))  # a .heic that's really a JPEG
        self.assertEqual(self.get("/api/trips/../../etc")[0], 404)

    def test_check_returns_every_image_and_the_verdict(self):
        self.jev.by_colour = {COLOURS[5]: [LOADING], COLOURS[6]: [UNLOADING], COLOURS[7]: [SIDEWAYS, FINAL]}
        status, body = self.post({"vehicle_number": "kl 05 af 9341", "model": "jev-latest", "quantity": "2,570 kg",
                                  "min_gap_tare_first": "60", "clock_slack": "10",
                                  "images": {"loading": [self.image(COLOURS[5])], "unloading": [self.image(COLOURS[6])],
                                             "bill": [self.image(COLOURS[7])]}})
        self.assertEqual(status, 200, body)
        self.assertEqual((body["vehicle_number"], body["quantity"]), (NUMBER, QUANTITY))
        self.assertEqual(body["limits"], {"min_gap_tare_first": 60, "min_gap_gross_first": 10, "clock_slack": 10, "max_overnight_gap": 720,
                                          "plant_radius": 500})
        self.assertIsNone(body["plant"])
        self.assertNotIn("unloading_plant", by_key(body["verdict"]["checks"]))
        self.assertEqual(body["verdict"]["title"], "Accepted", body["verdict"]["flags"])
        self.assertEqual({p: x["title"] for p, x in body["verdict"]["parts"].items()}, {"real": "Accepted", "docs": "Accepted"})
        self.assertEqual(body["images"]["bill"][0]["rotated"], 90)
        self.assertEqual(by_key(body["verdict"]["checks"])["reused"]["status"], "pass")
        for role in proof_check.ROLES:
            self.assertTrue(body["images"][role][0]["sent_image"].startswith("data:image/jpeg;base64,"))

    def test_images_of_a_known_trip_are_flagged_unless_it_is_that_trip(self):
        self.jev.by_colour = {COLOURS[0]: [LOADING], COLOURS[6]: [UNLOADING], COLOURS[7]: [FINAL]}
        body = {"vehicle_number": NUMBER, "model": "jev-latest", "quantity": QUANTITY,
                "images": {"loading": [self.image(COLOURS[0])], "unloading": [self.image(COLOURS[6])], "bill": [self.image(COLOURS[7])]}}
        status, result = self.post(body)
        self.assertEqual(by_key(result["verdict"]["checks"])["reused"]["detail"], "x.jpg is also in trip 171773. The same image can't be proof for two trips.")
        status, result = self.post({**body, "trip_id": "day/171773"})
        self.assertEqual(by_key(result["verdict"]["checks"])["reused"]["status"], "pass")

    def test_needs_a_vehicle_number(self):
        status, body = self.post({"vehicle_number": "", "images": {"loading": [self.image(COLOURS[0])]}, "model": "jev-latest"})
        self.assertEqual(status, 400)
        self.assertIn("vehicle number", body["error"])

    def test_rejects_limits_that_are_not_whole_minutes(self):
        status, body = self.post({"vehicle_number": NUMBER, "clock_slack": "five", "model": "jev-latest",
                                  "images": {"loading": [self.image(COLOURS[0])]}})
        self.assertEqual(status, 400)
        self.assertIn("whole minutes", body["error"])

    def test_checks_the_plant_given(self):
        self.jev.by_colour = {COLOURS[8]: [{**UNLOADING, **PULATH}]}
        status, body = self.post({"vehicle_number": NUMBER, "model": "jev-latest", "plant": "11.4404, 75.9420",
                                  "plant_radius": "300", "images": {"unloading": [self.image(COLOURS[8])]}})
        self.assertEqual(status, 200, body)
        self.assertEqual((body["plant"], body["limits"]["plant_radius"]), ([11.4404, 75.942], 300))
        c = by_key(body["verdict"]["checks"])["unloading_plant"]
        self.assertEqual((c["status"], c["detail"]), ("fail", "x.jpg is stamped at 11.1649, 76.1549, 38 km from the plant, but unloading must be within 300 m of it."))

    def test_checks_the_weighbridge_stamp(self):
        self.jev.by_colour = {COLOURS[9]: [{**FINAL, "weighbridge_seal": 0.04}]}
        status, body = self.post({"vehicle_number": NUMBER, "quantity": QUANTITY, "model": "jev-latest", "verra": True,
                                  "images": {"bill": [self.image(COLOURS[9])]}})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["verra"])
        c = by_key(body["verdict"]["checks"])
        self.assertEqual((c["weighbridge_seal"]["status"], c["weighbridge_seal"]["detail"]), ("fail", "x.jpg: no weighbridge stamp found. The final slip must carry the weighbridge's own stamp."))
        self.assertEqual(body["verdict"]["parts"]["verra"]["status"], "problem")

    def test_rejects_a_plant_that_is_not_a_position(self):
        status, body = self.post({"vehicle_number": NUMBER, "plant": "Raroth", "model": "jev-latest",
                                  "images": {"loading": [self.image(COLOURS[0])]}})
        self.assertEqual(status, 400)
        self.assertIn("latitude, longitude", body["error"])

    def test_rejects_a_quantity_that_is_not_a_number(self):
        status, body = self.post({"vehicle_number": NUMBER, "quantity": "lots", "model": "jev-latest",
                                  "images": {"loading": [self.image(COLOURS[0])]}})
        self.assertEqual(status, 400)
        self.assertIn("quantity in kilograms", body["error"])

    def test_needs_an_image(self):
        status, body = self.post({"vehicle_number": NUMBER, "model": "jev-latest", "images": {}})
        self.assertEqual(status, 400)
        self.assertIn("Add the loading photos", body["error"])

    def test_rejects_files_that_are_not_images(self):
        status, body = self.post({"vehicle_number": NUMBER, "model": "jev-latest",
                                  "images": {"bill": [{"name": "a.jpg", "data": base64.b64encode(b"hello").decode()}]}})
        self.assertEqual(status, 400)
        self.assertIn("JPEG or PNG", body["error"])

    def test_rejects_unknown_models(self):
        status, body = self.post({"vehicle_number": NUMBER, "model": "gpt-4"})
        self.assertEqual(status, 400)
        self.assertIn("Unknown model", body["error"])


if __name__ == "__main__":
    unittest.main()
