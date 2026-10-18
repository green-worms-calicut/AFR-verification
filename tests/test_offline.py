"""Offline tests: run check_proof.py and app.py against a fake Jev API. No API key or network needed.

Run:
    .venv/bin/python -m unittest discover -s tests -v
"""

import base64
import io
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import httpx2
from PIL import Image as PILImage

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
import check_proof  # noqa: E402
import evaluate  # noqa: E402
from typesafe_sdk import TypeSafeClient  # noqa: E402


def fake_answers(picks: dict[str, tuple[str, float]]) -> dict:
    """A System One response body: the given (choice, confidence) per question, a low-confidence first option otherwise."""
    answers = {}
    for qid, question in check_proof.QUESTIONS.items():
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


SIDEWAYS_BILL = {"kind": ("bill", 0.9), "orientation": ("top_points_right", 0.93)}
UPRIGHT_BILL = {
    "kind": ("bill", 0.97), "orientation": ("upright", 0.95),
    "in_hour": ("03", 0.92), "in_minute": ("09", 0.55), "out_hour": ("08", 0.9), "out_minute": ("57", 0.86),
    "bill_day": ("15", 0.94), "bill_month": ("10", 0.96),
}
PHOTO = {
    "kind": ("geotagged_photo", 0.98), "orientation": ("upright", 0.97),
    "stamp_hour": ("04", 0.93), "stamp_minute": ("09", 0.88), "stamp_day": ("15", 0.95), "stamp_month": ("10", 0.97),
}


class FakeJev:
    """Replies to each request with the next canned response, and records what was sent."""

    def __init__(self, *responses: dict):
        self.responses = list(responses)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return httpx2.Response(200, json=fake_answers(self.responses.pop(0)))

    def client(self) -> TypeSafeClient:
        return TypeSafeClient(api_key="test", headers=check_proof.HEADERS, transport=httpx2.MockTransport(self))


def sample(name: str) -> PILImage.Image:
    return PILImage.open(ROOT / "samples" / name).convert("RGB")


class FitTests(unittest.TestCase):
    def test_sizes_stay_within_api_limits(self):
        for size in [(346, 588), (4000, 3000), (100, 100), (3000, 200)]:
            w, h = check_proof.fit(PILImage.new("RGB", size)).size
            with self.subTest(size=size):
                self.assertEqual(max(w, h), check_proof.MAX_SIDE)
                self.assertGreaterEqual(min(w, h), check_proof.MIN_SIDE)


class AnalyzeTests(unittest.TestCase):
    def test_sideways_bill_is_rotated_and_asked_again(self):
        jev = FakeJev(SIDEWAYS_BILL, UPRIGHT_BILL)
        with jev.client() as client:
            result, sent = check_proof.analyze(client, sample("bill.png"), "jev-latest")

        self.assertEqual(len(jev.requests), 2)
        self.assertEqual(jev.requests[0].headers["TypeSafe-Danger-Zone-Images"], "true")
        self.assertEqual(result["rotated"], 90)
        self.assertGreater(sent.width, sent.height)  # portrait photo of a landscape slip, now landscape
        self.assertEqual(result["kind"]["choice"], "bill")
        readings = {r["key"]: r for r in result["readings"]}
        self.assertEqual(readings["time_in"]["value"], "03:09")
        self.assertEqual(readings["time_out"]["value"], "08:57")
        self.assertEqual(readings["bill_date"]["value"], "15/10")
        self.assertAlmostEqual(readings["time_in"]["min_confidence"], 0.55)
        self.assertEqual(result["input_tokens"], 2000)

    def test_geotagged_photo_needs_one_request(self):
        jev = FakeJev(PHOTO)
        with jev.client() as client:
            result, _ = check_proof.analyze(client, sample("photo.png"), "jev-latest")

        self.assertEqual(len(jev.requests), 1)
        self.assertEqual(result["rotated"], 0)
        self.assertEqual([r["value"] for r in result["readings"]], ["04:09", "15/10"])

    def test_other_images_report_no_readings(self):
        jev = FakeJev({"kind": ("other", 0.9), "orientation": ("upright", 0.9)})
        with jev.client() as client:
            result, _ = check_proof.analyze(client, sample("analog-1042.jpg"), "jev-latest")
        self.assertEqual(result["readings"], [])


class QuestionTests(unittest.TestCase):
    def test_time_and_date_questions_state_their_premise_and_allow_not_shown(self):
        for qid, question in check_proof.QUESTIONS.items():
            if qid in ("kind", "orientation"):
                continue
            with self.subTest(qid=qid):
                self.assertTrue(question.instructions.startswith((check_proof.BILL, check_proof.PHOTO)))
                self.assertIn(check_proof.NOT_SHOWN, question.criteria)

    def test_not_shown_part_makes_the_reading_missing(self):
        jev = FakeJev({**UPRIGHT_BILL, "out_hour": ("not_shown", 0.9)})
        with jev.client() as client:
            result, _ = check_proof.analyze(client, sample("bill.png"), "jev-latest")
        readings = {r["key"]: r for r in result["readings"]}
        self.assertTrue(readings["time_out"]["missing"])
        self.assertIsNone(readings["time_out"]["value"])
        self.assertFalse(readings["time_in"]["missing"])


class EvaluateTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(evaluate.normalize("time_in", "3:09"), "03:09")
        self.assertEqual(evaluate.normalize("photo_date", "15/10/2026"), "15/10")
        self.assertEqual(evaluate.normalize("bill_date", "5.1.2026"), "05/01")
        self.assertEqual(evaluate.normalize("time_out", " Not_Shown "), "not_shown")
        self.assertEqual(evaluate.normalize("kind", "Bill"), "bill")

    def test_scores_and_reports_example_labels(self):
        rows = evaluate.load_labels(ROOT / "labels.example.csv")
        rows[0]["time_out"] = "09:57"  # a deliberately wrong label, to see a mistake reported
        jev = FakeJev(SIDEWAYS_BILL, UPRIGHT_BILL, PHOTO, {"kind": ("other", 0.9), "orientation": ("upright", 0.9)})
        with jev.client() as client:
            results = evaluate.run(client, rows, "jev-latest")
        items = evaluate.score(rows, results)

        by_field = {(i["file"], i["field"]): i for i in items}
        self.assertEqual(len(items), 8)  # 3 kinds + 3 bill readings + 2 photo readings
        self.assertTrue(by_field[("samples/bill.png", "time_in")]["correct"])
        self.assertFalse(by_field[("samples/bill.png", "time_out")]["correct"])
        self.assertTrue(by_field[("samples/photo.png", "photo_time")]["correct"])
        self.assertTrue(by_field[("samples/analog-1042.jpg", "kind")]["correct"])

        text = evaluate.report(items, results, rows, "jev-latest")
        self.assertIn("Mistakes (1)", text)
        self.assertIn("09:57", text)
        self.assertIn("accepted but wrong", text)


class AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.jev = FakeJev()
        cls.real_client = app.TypeSafeClient
        app.TypeSafeClient = lambda **kw: cls.real_client(api_key="test", transport=httpx2.MockTransport(cls.jev), **kw)
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

    def test_page_and_status(self):
        with urllib.request.urlopen(self.base + "/") as response:
            self.assertIn(b"Proof Check Bench", response.read())
        with urllib.request.urlopen(self.base + "/api/status") as response:
            self.assertTrue(json.load(response)["key_set"])

    def test_check_returns_readings_and_preview(self):
        self.jev.responses.append(PHOTO)
        data = base64.b64encode((ROOT / "samples" / "photo.png").read_bytes()).decode()
        status, body = self.post({"name": "photo.png", "data": "data:image/png;base64," + data, "model": "jev-latest"})
        self.assertEqual(status, 200)
        self.assertEqual(body["kind"]["choice"], "geotagged_photo")
        self.assertTrue(body["sent_image"].startswith("data:image/jpeg;base64,"))

    def test_rejects_files_that_are_not_images(self):
        status, body = self.post({"data": base64.b64encode(b"hello").decode(), "model": "jev-latest"})
        self.assertEqual(status, 400)
        self.assertIn("JPEG or PNG", body["error"])

    def test_rejects_unknown_models(self):
        status, body = self.post({"data": "", "model": "gpt-4"})
        self.assertEqual(status, 400)
        self.assertIn("Unknown model", body["error"])


if __name__ == "__main__":
    unittest.main()
