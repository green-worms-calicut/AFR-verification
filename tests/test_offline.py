"""Offline tests: run check_proof.py and app.py against a fake Jev API. No API key or network needed.

Run:
    .venv/bin/python -m unittest discover -s tests -v
"""

import base64
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from compression import zstd
from pathlib import Path
from unittest import mock

import httpx2
from PIL import Image as PILImage

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
import check_proof  # noqa: E402
import evaluate  # noqa: E402
from typesafe_sdk import Noul, TypeSafeClient  # noqa: E402

NUMBER = "KL05AF9341"


def fake_answers(slot: str, picks: dict) -> dict:
    """A System One response body for one slot's questions.

    `picks` gives a yes probability per yes/no question and a (choice, confidence) per Choice.
    Anything not picked gets an unsure answer: 0.5, or the first option at confidence 0.3.
    """
    answers = {}
    for qid, question in check_proof.QUESTIONS[slot].items():
        if isinstance(question, Noul):
            answers[qid] = {"type": "noul", "noul": picks.get(qid, 0.5)}
            continue
        options = list(question.criteria)
        choice, conf = picks.get(qid, (options[0], 0.3))
        rest = (1 - conf) / (len(options) - 1)
        answers[qid] = {
            "type": "choice",
            "choice": choice,
            "confidence": conf,
            "probabilities": {o: conf if o == choice else rest for o in options},
        }
    return {"model": "jev-1.13.0", "answers": answers, "usage": {"input_tokens": 1000, "output_tokens": 30}}


PHOTO = {
    "orientation": ("upright", 0.97), "shows_vehicle": 0.97, "gps_stamp": 0.95,
    "plate": ("readable", 0.9), "plate_number": ("same", 0.88),
    "stamp_hour": ("04", 0.93), "stamp_minute": ("09", 0.88), "stamp_day": ("15", 0.95), "stamp_month": ("10", 0.97),
}
SIDEWAYS_BILL = {"orientation": ("top_points_right", 0.93)}
BILL = {
    "orientation": ("upright", 0.95), "weigh_bill": 0.96, "bill_number": ("same", 0.9),
    "bill_day": ("15", 0.94), "bill_month": ("10", 0.96),
}
NOT_A_VEHICLE = {"orientation": ("upright", 0.9), "shows_vehicle": 0.03, "gps_stamp": 0.04,
                 "plate": ("not_visible", 0.95), "plate_number": ("cannot_tell", 0.9)}


def sent(request: httpx2.Request) -> bytes:
    """The request body as sent, before the SDK's zstd compression."""
    body = request.read()
    return zstd.decompress(body) if request.headers.get("content-encoding") == "zstd" else body


class FakeJev:
    """Replies to each request with the next canned answers for its slot, and records what was sent.

    The slot is told apart by the questions in the request, because app.py checks both images at once.
    """

    def __init__(self, photo=(), bill=()):
        self.responses = {"photo": list(photo), "bill": list(bill)}
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        slot = "bill" if b"weigh_bill" in sent(request) else "photo"
        return httpx2.Response(200, json=fake_answers(slot, self.responses[slot].pop(0)))

    def client(self) -> TypeSafeClient:
        return TypeSafeClient(api_key="test", headers=check_proof.HEADERS, transport=httpx2.MockTransport(self))


def sample(name: str) -> PILImage.Image:
    return PILImage.open(ROOT / "samples" / name).convert("RGB")


def analyze(slot: str, *responses: dict, image: str | None = None, number: str = NUMBER):
    jev = FakeJev(**{slot: responses})
    with jev.client() as client:
        result, sent = check_proof.analyze(client, slot, sample(image or f"{slot}.png"), number, "jev-latest")
    return result, sent, jev


def by_key(items: list[dict]) -> dict[str, dict]:
    return {i["key"]: i for i in items}


class ApiKeyTests(unittest.TestCase):
    def setUp(self):
        check_proof._keyring_key = None
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(setattr, check_proof, "_keyring_key", None)
        os.environ.pop("TYPESAFE_API_KEY", None)

    def test_environment_variable_comes_first(self):
        os.environ["TYPESAFE_API_KEY"] = "from-env"
        with mock.patch("subprocess.run") as run:
            self.assertEqual(check_proof.api_key(), "from-env")
        run.assert_not_called()

    def test_reads_the_keyring_once(self):
        found = mock.Mock(stdout="from-keyring\n")
        with mock.patch("subprocess.run", return_value=found) as run:
            self.assertEqual(check_proof.api_key(), "from-keyring")
            self.assertEqual(check_proof.api_key(), "from-keyring")
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], check_proof.KEY_LOOKUP)

    def test_no_key_when_not_stored_or_no_keyring(self):
        with mock.patch("subprocess.run", return_value=mock.Mock(stdout="")):
            self.assertIsNone(check_proof.api_key())
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            self.assertIsNone(check_proof.api_key())


class FitTests(unittest.TestCase):
    def test_sizes_stay_within_api_limits(self):
        for size in [(346, 588), (4000, 3000), (100, 100), (3000, 200)]:
            w, h = check_proof.fit(PILImage.new("RGB", size)).size
            with self.subTest(size=size):
                self.assertEqual(max(w, h), check_proof.MAX_SIDE)
                self.assertGreaterEqual(min(w, h), check_proof.MIN_SIDE)


class VehicleNumberTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(check_proof.normalize_vehicle_number(" kl 05 af-9341 "), NUMBER)
        self.assertEqual(check_proof.normalize_vehicle_number("KL.05.AF.9341"), NUMBER)
        self.assertIsNone(check_proof.normalize_vehicle_number(""))
        self.assertIsNone(check_proof.normalize_vehicle_number("KL0"))
        self.assertIsNone(check_proof.normalize_vehicle_number("KL05AF9341/2"))


class TripTimeTests(unittest.TestCase):
    def test_parse_time(self):
        self.assertEqual(check_proof.parse_time("9:05"), "09:05")
        self.assertEqual(check_proof.parse_time(" 23.59 "), "23:59")
        for bad in ["", None, "24:00", "9:60", "905", "9:5"]:
            with self.subTest(bad=bad):
                self.assertIsNone(check_proof.parse_time(bad))

    def test_parse_window_needs_both_times_or_neither(self):
        self.assertEqual(check_proof.parse_window("3:00", "09:00"), ("03:00", "09:00"))
        self.assertIsNone(check_proof.parse_window("", None))
        for arrival, end in [("03:00", ""), ("", "09:00"), ("03:00", "nine")]:
            with self.subTest(arrival=arrival, end=end), self.assertRaises(ValueError):
                check_proof.parse_window(arrival, end)

    def test_within_runs_past_midnight_when_the_end_is_earlier(self):
        self.assertTrue(check_proof.within("04:09", "03:00", "09:00"))
        self.assertFalse(check_proof.within("09:09", "03:00", "09:00"))
        self.assertTrue(check_proof.within("23:30", "22:00", "05:00"))
        self.assertTrue(check_proof.within("04:09", "22:00", "05:00"))
        self.assertFalse(check_proof.within("12:00", "22:00", "05:00"))


class AnalyzeTests(unittest.TestCase):
    def test_sideways_bill_is_rotated_and_asked_again(self):
        result, sent, jev = analyze("bill", SIDEWAYS_BILL, BILL)

        self.assertEqual(len(jev.requests), 2)
        self.assertEqual(jev.requests[0].headers["TypeSafe-Danger-Zone-Images"], "true")
        self.assertEqual(result["rotated"], 90)
        self.assertGreater(sent.width, sent.height)  # portrait photo of a landscape slip, now landscape
        self.assertEqual([r["value"] for r in result["readings"]], ["15/10"])
        self.assertEqual(result["input_tokens"], 2000)

    def test_vehicle_number_is_sent_in_the_state(self):
        _, _, jev = analyze("photo", PHOTO)
        self.assertIn(b'"vehicle_number":"KL05AF9341"', sent(jev.requests[0]).replace(b" ", b""))

    def test_unsure_orientation_is_not_rotated(self):
        result, _, jev = analyze("photo", {**PHOTO, "orientation": ("top_points_left", 0.4)})
        self.assertEqual(len(jev.requests), 1)
        self.assertEqual(result["rotated"], 0)

    def test_rotates_at_the_lower_rotation_cut_off(self):
        # 0.56 is what the real Jev gave the sideways sample bill: below CONFIDENT, above ROTATE.
        result, _, jev = analyze("bill", {**BILL, "orientation": ("top_points_right", 0.56)}, BILL)
        self.assertEqual(len(jev.requests), 2)
        self.assertEqual(result["rotated"], 90)

    def test_good_photo_passes_every_check(self):
        result, _, _ = analyze("photo", PHOTO)
        checks = by_key(result["checks"])
        self.assertEqual(list(checks), ["shows_vehicle", "gps_stamp", "plate", "plate_number"])
        self.assertTrue(all(c["status"] == "pass" for c in checks.values()))
        self.assertEqual(checks["plate_number"]["label"], f"Plate matches {NUMBER}")
        self.assertEqual([r["value"] for r in result["readings"]], ["04:09", "15/10"])

    def test_not_shown_part_makes_the_reading_missing(self):
        result, _, _ = analyze("photo", {**PHOTO, "stamp_hour": ("not_shown", 0.9)})
        readings = by_key(result["readings"])
        self.assertTrue(readings["photo_time"]["missing"])
        self.assertIsNone(readings["photo_time"]["value"])
        self.assertFalse(readings["photo_date"]["missing"])


class CheckTests(unittest.TestCase):
    def photo_checks(self, **picks):
        return by_key(analyze("photo", {**PHOTO, **picks})[0]["checks"])

    def test_yes_no_thresholds(self):
        for yes, status in [(0.95, "pass"), (0.8, "pass"), (0.5, "review"), (0.2, "fail"), (0.05, "fail")]:
            with self.subTest(yes=yes):
                self.assertEqual(self.photo_checks(gps_stamp=yes)["gps_stamp"]["status"], status)

    def test_plate_outcomes(self):
        self.assertEqual(self.photo_checks(plate=("partly_readable", 0.9))["plate"]["status"], "review")
        self.assertEqual(self.photo_checks(plate=("not_visible", 0.9))["plate"]["status"], "fail")
        self.assertEqual(self.photo_checks(plate_number=("different", 0.92))["plate_number"]["status"], "fail")
        self.assertEqual(self.photo_checks(plate_number=("cannot_tell", 0.92))["plate_number"]["status"], "review")

    def test_unsure_choice_goes_to_a_person(self):
        check = self.photo_checks(plate_number=("same", 0.6))["plate_number"]
        self.assertEqual(check["status"], "review")
        self.assertTrue(check["detail"].startswith("Unsure"))

    def test_bill_number(self):
        checks = by_key(analyze("bill", BILL)[0]["checks"])
        self.assertEqual(checks["weigh_bill"]["status"], "pass")
        self.assertEqual(checks["bill_number"]["detail"], f"Bill shows {NUMBER}")
        checks = by_key(analyze("bill", {**BILL, "bill_number": ("different", 0.9)})[0]["checks"])
        self.assertEqual(checks["bill_number"]["status"], "fail")


WINDOW = ("03:00", "09:00")


class VerdictTests(unittest.TestCase):
    def trip(self, photo=PHOTO, bill=BILL, window=WINDOW):
        jev = FakeJev(photo=[photo] if photo else [], bill=[bill] if bill else [])
        images = {slot: sample(f"{slot}.png") for slot, picks in (("photo", photo), ("bill", bill)) if picks}
        with jev.client() as client:
            trip, _ = check_proof.analyze_trip(client, images, NUMBER, "jev-latest", window)
        return trip

    def test_matching_trip_is_accepted(self):
        v = self.trip()["verdict"]
        self.assertEqual(v["status"], "pass")
        self.assertEqual(v["title"], "Accepted")
        trip = by_key(v["checks"])
        self.assertEqual(trip["same_date"]["detail"], "Both dated 15/10")
        self.assertEqual(trip["in_window"]["detail"], "Photo 04:09 is between arrival 03:00 and end 09:00")

    def test_bill_times_are_not_asked(self):
        # The vehicle can be weighed before loading, so only the bill's date is read.
        self.assertEqual([key for key, *_ in check_proof.REPORT["bill"]], ["bill_date"])
        self.assertFalse([q for q in check_proof.QUESTIONS["bill"] if "hour" in q or "minute" in q])

    def test_window_past_midnight(self):
        self.assertEqual(self.trip(window=("22:00", "05:00"))["verdict"]["status"], "pass")
        self.assertEqual(self.trip(window=("05:00", "22:00"))["verdict"]["status"], "fail")

    def test_no_window_typed_needs_a_person(self):
        v = self.trip(window=None)["verdict"]
        self.assertEqual(v["status"], "review")
        self.assertEqual(by_key(v["checks"])["in_window"]["detail"], "Arrival and end time not typed")

    def test_different_dates_are_rejected(self):
        v = self.trip(bill={**BILL, "bill_day": ("16", 0.95)})["verdict"]
        self.assertEqual(v["status"], "fail")
        self.assertEqual(by_key(v["checks"])["same_date"]["detail"], "Photo 15/10, bill 16/10")

    def test_wrong_plate_is_rejected(self):
        self.assertEqual(self.trip(photo={**PHOTO, "plate_number": ("different", 0.9)})["verdict"]["status"], "fail")

    def test_wrong_bill_number_is_rejected(self):
        self.assertEqual(self.trip(bill={**BILL, "bill_number": ("different", 0.9)})["verdict"]["status"], "fail")

    def test_photo_outside_the_trip_is_rejected(self):
        v = self.trip(photo={**PHOTO, "stamp_hour": ("09", 0.95)})["verdict"]
        self.assertEqual(v["status"], "fail")
        self.assertEqual(by_key(v["checks"])["in_window"]["detail"], "Photo 09:09 is outside arrival 03:00 to end 09:00")

    def test_unsure_check_needs_a_person(self):
        self.assertEqual(self.trip(photo={**PHOTO, "shows_vehicle": 0.6})["verdict"]["status"], "review")
        self.assertEqual(self.trip(photo={**PHOTO, "stamp_minute": ("09", 0.55)})["verdict"]["status"], "review")
        self.assertEqual(self.trip(bill={**BILL, "bill_day": ("15", 0.55)})["verdict"]["status"], "review")

    def test_unreadable_time_needs_a_person(self):
        v = self.trip(photo={**PHOTO, "stamp_minute": ("not_shown", 0.9)})["verdict"]
        self.assertEqual(v["status"], "review")
        self.assertIn("photo time", by_key(v["checks"])["in_window"]["detail"])

    def test_one_image_waits_unless_it_already_fails(self):
        self.assertEqual(self.trip(bill=None)["verdict"]["status"], "waiting")
        self.assertEqual(self.trip(photo=None)["verdict"]["status"], "waiting")
        self.assertEqual(self.trip(photo=NOT_A_VEHICLE, bill=None)["verdict"]["status"], "fail")
        self.assertEqual(self.trip(photo={**PHOTO, "stamp_hour": ("12", 0.95)}, bill=None)["verdict"]["status"], "fail")


class QuestionTests(unittest.TestCase):
    def test_time_and_date_questions_state_their_premise_and_allow_not_shown(self):
        for slot, prefix in (("photo", check_proof.STAMP), ("bill", check_proof.BILL)):
            for qid in [q for _, _, qids, _ in check_proof.REPORT[slot] for q in qids]:
                question = check_proof.QUESTIONS[slot][qid]
                with self.subTest(qid=qid):
                    self.assertTrue(question.instructions.startswith(prefix))
                    self.assertIn(check_proof.NOT_SHOWN, question.criteria)

    def test_number_questions_compare_with_the_typed_number_and_never_lead_with_same(self):
        for slot, qid in (("photo", "plate_number"), ("bill", "bill_number")):
            question = check_proof.QUESTIONS[slot][qid]
            with self.subTest(qid=qid):
                self.assertIn("`vehicle_number`", question.instructions)
                self.assertIn("same", question.criteria)
                self.assertNotEqual(next(iter(question.criteria)), "same")

    def test_every_check_has_an_outcome_for_every_option(self):
        for qid, (_, outcomes) in check_proof.CHOICE_CHECKS.items():
            slot = "bill" if qid == "bill_number" else "photo"
            self.assertEqual(set(outcomes), set(check_proof.QUESTIONS[slot][qid].criteria), qid)


class EvaluateTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(evaluate.normalize("photo_time", "4:09"), "04:09")
        self.assertEqual(evaluate.normalize("photo_date", "15/10/2026"), "15/10")
        self.assertEqual(evaluate.normalize("bill_date", "5.1.2026"), "05/01")
        self.assertEqual(evaluate.normalize("photo_time", " Not_Shown "), "not_shown")
        self.assertEqual(evaluate.normalize("gps_stamp", "Yes"), "yes")

    def test_scores_and_reports_example_labels(self):
        rows = evaluate.load_labels(ROOT / "labels.example.csv")
        rows[2]["bill_date"] = "16/10/2026"  # a deliberately wrong label, to see a mistake reported
        wrong_number_photo = {**PHOTO, "plate_number": ("different", 0.85)}
        wrong_number_bill = {**BILL, "bill_number": ("different", 0.8)}
        clock = {**NOT_A_VEHICLE, "stamp_hour": ("not_shown", 0.9), "stamp_minute": ("not_shown", 0.9),
                 "stamp_day": ("not_shown", 0.9), "stamp_month": ("not_shown", 0.9)}
        jev = FakeJev(photo=[PHOTO, wrong_number_photo, clock], bill=[BILL, wrong_number_bill])
        with jev.client() as client:
            results = evaluate.run(client, rows, "jev-latest")
        items = evaluate.score(rows, results)

        by_field = {(i["row"], i["field"]): i for i in items}
        self.assertEqual(len(items), 17)  # 6 + 1 photo, 3 + 1 bill, 6 for the clock
        self.assertTrue(by_field[("samples/photo.png | photo | KL05AF9341", "plate_number")]["correct"])
        self.assertTrue(by_field[("samples/photo.png | photo | KL05AF9314", "plate_number")]["correct"])
        self.assertTrue(by_field[("samples/bill.png | bill | KL05AF9314", "bill_number")]["correct"])
        self.assertFalse(by_field[("samples/bill.png | bill | KL05AF9341", "bill_date")]["correct"])
        clock_vehicle = by_field[("samples/analog-1042.jpg | photo | KL05AF9341", "shows_vehicle")]
        self.assertEqual((clock_vehicle["got"], clock_vehicle["confidence"]), ("no", 0.97))

        text = evaluate.report(items, results, rows, "jev-latest")
        self.assertIn("Mistakes (1)", text)
        self.assertIn("16/10", text)
        self.assertIn("vehicle number: what each threshold would do", text)

    def test_rejects_a_field_the_slot_does_not_check(self):
        bad = Path(self.id() + ".csv")
        try:
            bad.write_text("file,slot,vehicle_number,plate\nsamples/bill.png,bill,KL05AF9341,readable\n")
            with self.assertRaises(SystemExit) as caught:
                evaluate.load_labels(bad)
            self.assertIn("isn't checked on a bill", str(caught.exception))
        finally:
            bad.unlink()


class AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.jev = FakeJev()
        cls.real_client = app.TypeSafeClient
        app.TypeSafeClient = lambda **kw: cls.real_client(**{**kw, "api_key": "test"}, transport=httpx2.MockTransport(cls.jev))
        cls.old_key = os.environ.get("TYPESAFE_API_KEY")
        os.environ["TYPESAFE_API_KEY"] = "test"
        cls.server = app.serve(0)  # any free port
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        app.TypeSafeClient = cls.real_client
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

    @staticmethod
    def image(name: str) -> str:
        return "data:image/png;base64," + base64.b64encode((ROOT / "samples" / name).read_bytes()).decode()

    def test_page_and_status(self):
        with urllib.request.urlopen(self.base + "/") as response:
            self.assertIn(b"Proof Check Bench", response.read())
        with urllib.request.urlopen(self.base + "/api/status") as response:
            self.assertTrue(json.load(response)["key_set"])

    def test_check_returns_both_results_and_the_verdict(self):
        self.jev.responses["photo"].append(PHOTO)
        self.jev.responses["bill"].extend([SIDEWAYS_BILL, BILL])
        status, body = self.post({"vehicle_number": "kl 05 af 9341", "model": "jev-latest", "arrival": "03:00", "end": "09:00",
                                  "photo": self.image("photo.png"), "bill": self.image("bill.png")})
        self.assertEqual(status, 200)
        self.assertEqual(body["vehicle_number"], NUMBER)
        self.assertEqual(body["window"], ["03:00", "09:00"])
        self.assertEqual(body["verdict"]["title"], "Accepted")
        self.assertEqual(body["bill"]["rotated"], 90)
        for slot in check_proof.SLOTS:
            self.assertTrue(body[slot]["sent_image"].startswith("data:image/jpeg;base64,"))

    def test_needs_a_vehicle_number(self):
        status, body = self.post({"vehicle_number": "", "photo": self.image("photo.png"), "model": "jev-latest"})
        self.assertEqual(status, 400)
        self.assertIn("vehicle number", body["error"])

    def test_needs_both_times_or_neither(self):
        status, body = self.post({"vehicle_number": NUMBER, "arrival": "03:00", "photo": self.image("photo.png"), "model": "jev-latest"})
        self.assertEqual(status, 400)
        self.assertIn("arrival and end time", body["error"])

    def test_needs_an_image(self):
        status, body = self.post({"vehicle_number": NUMBER, "model": "jev-latest"})
        self.assertEqual(status, 400)
        self.assertIn("Add the vehicle photo", body["error"])

    def test_rejects_files_that_are_not_images(self):
        status, body = self.post({"vehicle_number": NUMBER, "photo": base64.b64encode(b"hello").decode(), "model": "jev-latest"})
        self.assertEqual(status, 400)
        self.assertIn("JPEG or PNG", body["error"])

    def test_rejects_unknown_models(self):
        status, body = self.post({"vehicle_number": NUMBER, "model": "gpt-4"})
        self.assertEqual(status, 400)
        self.assertIn("Unknown model", body["error"])


if __name__ == "__main__":
    unittest.main()
