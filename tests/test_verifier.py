"""Offline tests of the verifier service: gw_sales' end-to-end cases (signed request in, signed result out,
duplicates, edits, wrong secrets), the outbox's retries and recovery, and disposals checked with proof_check,
against a fake gw_sales, a fake S3 (moto) and the fake Jev of test_offline.

The service tests need the dev Postgres (README: verifier); they are skipped when it isn't running.

Run:
    .venv/bin/python -m unittest discover -s tests -v
"""

import asyncio
import io
import json
import logging
import os
import sys
import tempfile
import time
import unittest
import uuid
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

import boto3
import httpx
import psycopg
from moto import mock_aws
from PIL import Image as PILImage

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import proof_check  # noqa: E402
from proof_check import Upload  # noqa: E402
from test_proof_check import (AT_PLANT, COLOURS, FINAL, FIRST, LOADING, NUMBER, PLANT, PULATH, QUANTITY,  # noqa: E402
                              UNLOADING, FakeJev, at, coloured, make_trip, sent, stamp)
from verifier import (api, compare_disposals, database, disposal_check, entry_checks, logs, migrate, s3_photos,  # noqa: E402
                      signatures, standards, tasks)
from verifier.settings import Settings  # noqa: E402

DB = os.environ.get("VERIFIER_TEST_DATABASE_URL", "postgresql://verifier@127.0.0.1:5433/verifier_test")
GW = "https://dev-sales.example"
BUCKET = "gw-photos"
SETTINGS = Settings(database_url=DB, webhook_secret="webhook-secret-of-32-characters-0", callback_secret="callback-secret-of-32-characters-",
                    api_key="gws_test_key", gw_sales_url=GW, s3_bucket=BUCKET, typesafe_api_key="test")
DISPOSAL = 1234
# Where gw_sales keeps each kind of image, and its evidence category.
STORED = {"bill": ("weighbill", "disposal/2026/10/15/"), "loading": ("collection", "collection-logs/2026/10/15/"),
          "unloading": ("unloading", "disposal/2026/10/15/")}
NOT_STORED = {"bill": [Upload("weighbill1.jpg", b"")], "loading": [Upload("collection1.jpg", b"")],
              "unloading": [Upload("unloading1.jpg", b"")]}  # listed, never put in S3


def key_of(kind: str, upload: Upload) -> str:
    return STORED[kind][1] + upload.name


def files(uploads: dict[str, list[Upload]]) -> dict[str, bytes]:
    """The uploads by s3Key, as S3 would hold them."""
    return {key_of(kind, u): u.data for kind, items in uploads.items() for u in items}


def disposal(uploads: dict[str, list[Upload]] = NOT_STORED, request_id: str | None = None, reason: str = "created",
             data_version: str = "sha256:aaa", logs: list[dict] = (), facility: dict | None = None,
             disposal_id: int = DISPOSAL, **claims) -> dict:
    """A request as gw_sales sends it, cut down to what the service reads, listing `uploads` as evidence."""
    owner = {"loading": {"type": "collection_log", "id": 45001}}
    return {
        "requestId": request_id or str(uuid.uuid4()), "reason": reason, "schemaVersion": 1,
        "subject": {"type": "disposal_log", "id": disposal_id, "createdVia": "DISPOSAL_ENTRY",
                    "createdAt": "2026-10-15T04:20:00.000Z", "dataVersion": data_version, "status": "approval_pending"},
        "callbackUrl": f"{GW}/api/procurement/disposal-logs/{disposal_id}/verification-results",
        "claims": {"arrivalTime": "2026-10-15T03:00:00.000Z", "quantity": float(QUANTITY), "unit": "kg",
                   "vehicle": {"id": 12, "vehicleNumber": "kl 05 af 9341"}, **claims},
        "evidence": [{"owner": owner.get(kind, {"type": "disposal_log", "id": disposal_id}), "category": STORED[kind][0],
                      "s3Key": key_of(kind, u), "mimeType": "image/jpeg", "uploadedAt": "2026-10-15T04:19:41.000Z"}
                     for kind, items in uploads.items() for u in items],
        "reference": {"facility": facility or {"id": 3, "location": None}, "collectionLogs": list(logs)},
    }


def other_trip(prefix: str = "b-", colours: int = 6, loading=LOADING, unloading=UNLOADING, bill=(FIRST, FINAL),
               quality: int = 90) -> tuple[dict[str, list[Upload]], FakeJev]:
    """Another trip of the same vehicle, its images named with `prefix`: in colours from COLOURS[colours] on
    (different images; 0 and another `quality` makes look-alikes of make_trip's), answered by its own fake Jev."""
    uploads, by_colour, n = {}, {}, colours
    pair = lambda picks: picks if isinstance(picks, tuple) else (picks, picks)  # one picks for both, or one each
    for kind, items, name in (("loading", pair(loading), "collection"), ("unloading", pair(unloading), "unloading"),
                              ("bill", bill, "weighbill")):
        uploads[kind] = []
        for i, picks in enumerate(items):
            by_colour[COLOURS[n]] = [picks]
            uploads[kind].append(Upload(f"{prefix}{name}{i + 1}.jpg", coloured(COLOURS[n], quality)))
            n += 1
    return uploads, FakeJev(by_colour=by_colour)


LATER = (FIRST, {**FINAL, **at("tare", "05:00"), **at("gross", "06:30")})  # weighed after make_trip's 02:00-03:30


def signed(request: dict, secret: str = SETTINGS.webhook_secret, now: float | None = None) -> tuple[bytes, dict]:
    body = json.dumps(request).encode()
    return body, {"Content-Type": "application/json", "X-GW-Event": api.EVENT,
                  "X-GW-Delivery": request["requestId"], **signatures.sign(body, secret, now)}


class FakeGwSales:
    """gw_sales' results endpoint: checks the key and signature as gw_sales does, then answers `statuses` in turn
    (200 once they run out); a status of None times out."""

    def __init__(self, *statuses: int | None):
        self.statuses = list(statuses)
        self.stored: dict[str, dict] = {}
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        body = request.content
        if request.headers.get("x-api-key") != SETTINGS.api_key or not signatures.verify(
                body, request.headers.get("x-gw-timestamp"), request.headers.get("x-gw-signature"),
                SETTINGS.callback_secret):
            return httpx.Response(401, json={"error": "unauthorised"})
        status = self.statuses.pop(0) if self.statuses else 200
        if status is None:
            raise httpx.ReadTimeout("timed out", request=request)
        if status != 200:
            return httpx.Response(status, json={"status": "error", "statusCode": status})
        result = json.loads(body)
        duplicate = result["requestId"] in self.stored
        self.stored[result["requestId"]] = result
        return httpx.Response(200, json={"ok": True, "duplicate": duplicate})


class SigningTests(unittest.TestCase):
    def test_signature_matches_the_contracts_example_code(self):
        # The contract's verify_gw_signature: HMAC-SHA256 over b"<timestamp>." + raw body, hex, "sha256=" prefix.
        import hashlib
        import hmac
        body, ts = b'{"a":1}', "1761545400"
        expected = "sha256=" + hmac.new(b"s", b"1761545400." + body, hashlib.sha256).hexdigest()
        self.assertEqual(signatures.signature(body, ts, "s"), expected)

    def test_verify(self):
        body, now = b'{"requestId":"x"}', 1_761_545_400
        headers = signatures.sign(body, "s", now)
        ts, sig = headers["X-GW-Timestamp"], headers["X-GW-Signature"]
        self.assertTrue(signatures.verify(body, ts, sig, "s", now=now + 299))
        self.assertFalse(signatures.verify(body, ts, sig, "s", now=now + 301), "too old")
        self.assertFalse(signatures.verify(body, ts, sig, "s", now=now - 301), "from the future")
        self.assertFalse(signatures.verify(body + b" ", ts, sig, "s", now=now), "body changed")
        self.assertFalse(signatures.verify(body, ts, sig, "other", now=now), "other secret")
        self.assertFalse(signatures.verify(body, "soon", sig, "s", now=now))
        self.assertFalse(signatures.verify(body, None, None, "s", now=now))
        self.assertFalse(signatures.verify(body, ts, "sha256=é", "s", now=now), "not ASCII: refused, not an error")


class SettingsTests(unittest.TestCase):
    ENV = {"VERIFIER_DATABASE_URL": DB, "VERIFIER_WEBHOOK_SECRET": "w" * 32, "VERIFIER_CALLBACK_SECRET": "c" * 32,
           "VERIFIER_API_KEY": "gws_test_key", "GW_SALES_URL": GW, "VERIFIER_S3_BUCKET": BUCKET, "TYPESAFE_API_KEY": "test"}

    def from_env(self, **changes):
        with mock.patch.dict(os.environ, {**self.ENV, **changes}):
            return Settings.from_env()

    def test_good_settings(self):
        self.assertEqual(self.from_env().gw_sales_url, GW)
        self.assertEqual(self.from_env(GW_SALES_URL="http://127.0.0.1:8001").gw_sales_url, "http://127.0.0.1:8001",
                         "http only on this machine, for tests")

    def test_results_are_only_sent_over_https(self):
        with self.assertRaises(SystemExit) as e:
            self.from_env(GW_SALES_URL="http://dev-sales.example")
        self.assertIn("GW_SALES_URL", str(e.exception))

    def test_secrets_are_long(self):
        for name in ("VERIFIER_WEBHOOK_SECRET", "VERIFIER_CALLBACK_SECRET", "VERIFIER_WEBHOOK_SECRET_NEXT"):
            with self.subTest(name=name):
                with self.assertRaises(SystemExit) as e:
                    self.from_env(**{name: "short"})
                self.assertIn(name, str(e.exception))


class DisposalTests(unittest.TestCase):
    """What proof_check is given from a request."""

    def test_entries_and_reference(self):
        logs = [{"id": 45001, "client": {"defaultProject": {"code": "VERRA"}}}]
        d = disposal_check.disposal(disposal(logs=logs, facility={"location": {"lat": 11.4404, "lng": 75.942}}), "VERRA")
        self.assertEqual((d.vehicle_number, d.quantity, d.plant, d.verra), (NUMBER, QUANTITY, (11.4404, 75.942), True))
        self.assertEqual(d.evidence, {"bill": ["disposal/2026/10/15/weighbill1.jpg"],
                                      "loading": ["collection-logs/2026/10/15/collection1.jpg"],
                                      "unloading": ["disposal/2026/10/15/unloading1.jpg"]})

    def test_verra_only_with_the_project_set_and_matching(self):
        logs = [{"client": {"defaultProject": {"code": "SHOP"}}}, {"client": {"defaultProject": {"code": "VERRA"}}}]
        self.assertTrue(disposal_check.disposal(disposal(logs=logs), "VERRA").verra, "a mix is checked as Verra")
        self.assertFalse(disposal_check.disposal(disposal(logs=logs), None).verra)
        self.assertFalse(disposal_check.disposal(disposal(logs=logs[:1]), "VERRA").verra)

    def test_quantity_in_bags_or_zero_is_not_checked(self):
        self.assertIsNone(disposal_check.disposal(disposal(quantity=12, unit="bags")).quantity)
        with self.assertLogs("verifier", "WARNING"):
            self.assertIsNone(disposal_check.disposal(disposal(quantity=0)).quantity)

    def test_other_categories_are_skipped(self):
        request = disposal()
        request["evidence"].append({"category": "gps_photo", "s3Key": "disposal/x.jpg"})
        with self.assertLogs("verifier", "INFO") as logs:
            d = disposal_check.disposal(request)
        self.assertNotIn("disposal/x.jpg", sum(d.evidence.values(), []))
        self.assertIn("gps_photo", logs.output[0])

    def test_trip_date_is_in_ist(self):
        self.assertEqual(disposal_check.disposal(disposal(arrivalTime="2026-10-27T19:00:00.000Z")).day, date(2026, 10, 28))
        self.assertEqual(disposal_check.disposal(disposal(arrivalTime=None)).day, date(2026, 10, 15), "from createdAt")
        with self.assertLogs("verifier", "WARNING"):
            self.assertEqual(standards.ist_day("2026-10-27 18:29:59"), date(2026, 10, 27), "no zone: taken as IST")

    def test_a_zone_less_time_is_read_as_ist(self):
        with self.assertLogs("verifier", "WARNING") as logs:
            at = standards.ist_time("2026-10-15T09:00:00")
        self.assertEqual((at.hour, at.minute, at.utcoffset().total_seconds()), (9, 0, 5.5 * 3600))
        self.assertIn("2026-10-15T09:00:00", logs.output[0])


class PhotoProblemTests(unittest.TestCase):
    """Which fetched files are flagged as unreadable instead of crashing the check."""

    def test_a_truncated_jpeg_is_unreadable(self):
        self.assertEqual(s3_photos.problem(coloured(COLOURS[0])[:-2000]), "isn't an image that can be read")

    def test_a_photo_too_large_to_open_is_unreadable(self):
        buffer = io.BytesIO()
        PILImage.new("RGB", (3000, 2000)).save(buffer, "JPEG", quality=50)
        with mock.patch.object(PILImage, "MAX_IMAGE_PIXELS", 1_000_000):
            self.assertEqual(s3_photos.problem(buffer.getvalue()), "isn't an image that can be read")

    def test_s3_busy_or_failing_is_retried_but_access_denied_is_not(self):
        from botocore.exceptions import ClientError

        def s3_answering(code, status):
            error = ClientError({"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}, "GetObject")
            return mock.Mock(get_object=mock.Mock(side_effect=error))

        job = mock.Mock(attempts=0)
        for code, status in (("SlowDown", 503), ("InternalError", 500), ("RequestTimeout", 400)):
            with self.subTest(code=code), self.assertRaises(s3_photos.S3Unavailable) as e:
                s3_photos.Photos(s3_answering(code, status), BUCKET).fetch("disposal/x.jpg")
            self.assertFalse(tasks.VERIFY_RETRY.gives_up(e.exception, job), "retried")
        with self.assertRaises(Exception) as e:
            s3_photos.Photos(s3_answering("AccessDenied", 403), BUCKET).fetch("disposal/x.jpg")
        self.assertTrue(tasks.VERIFY_RETRY.gives_up(e.exception, job), "our key's policy: retrying won't change it")

    def test_a_good_photo_has_no_problem(self):
        self.assertIsNone(s3_photos.problem(coloured(COLOURS[0])))


class StandardTests(unittest.TestCase):
    def test_the_standard_of_the_trips_day(self):
        later = standards.Standard("3", date(2026, 12, 1), proof_check.Limits(clock_slack=10))
        with mock.patch.object(standards, "STANDARDS", [*standards.STANDARDS, later]):
            self.assertEqual(standards.for_day(date(2026, 11, 30)).version, "2")
            self.assertEqual(standards.for_day(date(2026, 12, 1)).version, "3")
            self.assertEqual(standards.for_day(date(2020, 1, 1)).version, "2")

    def test_models_are_pinned(self):
        described = standards.STANDARDS[0].describe()
        self.assertEqual(described["models"], {"questions": "jev-1.14.0", "box": "jev-1.14.0"})
        self.assertEqual(described["limits"]["min_gap_tare_first"], 60)
        self.assertEqual((described["limits"]["plant_radius"], described["limits"]["collection_radius"]), (2000, 4000))


class CheckTests(unittest.TestCase):
    """A request's photos checked with proof_check, and its flags."""

    def check(self, uploads, jev, request=None, stored=None):
        stored = files(uploads) if stored is None else stored
        with jev.client() as client:
            return disposal_check.check(request or disposal(uploads), stored.get, client)

    def test_good_trip_passes(self):
        uploads, jev = make_trip()
        checked = self.check(uploads, jev)
        self.assertEqual((checked.outcome, checked.flags), ("passed", []))
        self.assertEqual(checked.standard["version"], "2")
        self.assertEqual(checked.standard["models"]["questions"], "jev-1.14.0")
        self.assertTrue(jev.requests)
        self.assertTrue(all(b'"model":"jev-1.14.0"' in sent(r) for r in jev.requests), "pinned models only")
        self.assertEqual(checked.extractions["parts"], {"real": "pass", "docs": "pass"})
        self.assertIn("disposal/2026/10/15/weighbill2.jpg", checked.extractions["images"])
        self.assertGreater(checked.cost_usd, 0)
        json.dumps(checked.analysis, default=str)

    def test_only_gw_sales_photo_folders_are_read(self):
        uploads, jev = make_trip()
        request = disposal(uploads)
        request["evidence"].append({**request["evidence"][-1], "s3Key": "secrets/x.jpg"})
        fetched = []
        stored = files(uploads)

        def fetch(key):
            fetched.append(key)
            return stored.get(key)

        with jev.client() as client:
            checked = disposal_check.check(request, fetch, client)
        self.assertNotIn("secrets/x.jpg", fetched)
        flag = next(f for f in checked.flags if f["code"] == "image_unavailable")
        self.assertEqual(flag["evidence"], ["secrets/x.jpg"])
        self.assertIn("isn't in one of gw_sales' photo folders", flag["message"])

    def test_flags_carry_code_evidence_and_severity(self):
        wrong = {**FINAL, "bill_number": ("different", 0.92)}
        uploads, jev = make_trip(bill=(FINAL, wrong))
        checked = self.check(uploads, jev)
        self.assertEqual(checked.outcome, "flagged")
        first = checked.flags[0]
        self.assertEqual((first["code"], first["severity"], first["kind"]), ("bill_number", "high", "problem found"))
        self.assertIn("disposal/2026/10/15/weighbill2.jpg", first["evidence"])
        self.assertEqual(first["title"], "Bills match KL05AF9341")
        self.assertIn("1234 weigh bill 2", first["message"])
        self.assertNotIn("disposal/2026", first["message"])
        self.assertEqual(checked.extractions["images"]["disposal/2026/10/15/weighbill2.jpg"]["label"], "weigh bill 2")
        self.assertEqual(checked.analysis["labels"]["collection-logs/2026/10/15/collection1.jpg"], "collection photo 1")

    def test_images_are_named_by_kind_in_the_order_listed(self):
        evidence = [{"category": c, "s3Key": k} for c, k in (("collection", "a"), ("weighbill", "b"), ("collection", "c"),
                                                              ("unloading", "d"), ("gps_photo", "e"), ("collection", "a"))]
        self.assertEqual(disposal_check.image_labels(evidence),
                         {"a": "collection photo 1", "b": "weigh bill", "c": "collection photo 2", "d": "unloading photo"})

    def test_missing_photo_asks_a_person_and_the_rest_is_checked(self):
        uploads, jev = make_trip()
        stored = files(uploads)
        gone = key_of("unloading", uploads["unloading"][0])
        del stored[gone]
        with self.assertLogs("verifier", "WARNING"):
            checked = self.check(uploads, jev, stored=stored)
        [flag] = checked.flags
        self.assertEqual((flag["code"], flag["severity"], flag["kind"]), ("image_unavailable", "recheck", "please check"))
        self.assertEqual(flag["evidence"], [gone])
        self.assertIn("isn't in storage", flag["message"])
        self.assertEqual(checked.extractions["unavailable"], [gone])
        self.assertEqual(checked.extractions["parts"]["real"], "pass", "the other unloading photo was checked")

    def test_no_bill_left_is_one_flag_not_no_bill_added(self):
        uploads, jev = make_trip()
        stored = files(uploads)
        for u in uploads["bill"]:
            stored[key_of("bill", u)] = b"not an image"
        with self.assertLogs("verifier", "WARNING"):
            checked = self.check(uploads, jev, stored=stored)
        codes = [c for f in checked.flags for c in f["checks"]]
        self.assertNotIn("bill_missing", codes)
        merged = next(f for f in checked.flags if f["code"] == "image_unavailable")
        self.assertIn("isn't an image", merged["message"])
        self.assertIn("It also affects", merged["message"])
        self.assertIn("bill_number", merged["checks"])
        self.assertEqual(merged["severity"], "recheck")

    def test_collection_photos_listed_but_not_stored_still_ask_a_person(self):
        uploads, jev = make_trip()
        stored = files(uploads)
        for u in uploads["loading"]:
            del stored[key_of("loading", u)]
        with self.assertLogs("verifier", "WARNING"):
            checked = self.check(uploads, jev, stored=stored)
        [flag] = checked.flags
        self.assertEqual((flag["code"], flag["kind"]), ("image_unavailable", "please check"))
        self.assertIn("isn't in storage", flag["message"])

    def test_nothing_in_storage_needs_no_jev(self):
        jev = FakeJev()
        with self.assertLogs("verifier", "WARNING"):
            checked = self.check(NOT_STORED, jev, stored={})
        self.assertEqual(jev.requests, [])
        self.assertEqual(checked.outcome, "flagged")
        self.assertTrue(all(f["kind"] == "please check" for f in checked.flags))


def log(log_id: int = 45001, picked_up: str = "2026-10-14T21:00:00.000Z", **kw) -> dict:
    """A collection log as gw_sales sends it; 14 Oct 21:00 UTC is 15 Oct 02:30 IST, the day make_trip's photos say."""
    return {"id": log_id, "createdVia": "COLLECTION_ENTRY", "collectionDate": picked_up, "client": {"name": "ABC Hotel"},
            "collectionPoint": {"name": "Main Kitchen", "location": None}, **kw}


MATERIALS = [{"materialName": "PET", "quantity": 120, "unit": "kg"}]


class EntryCheckTests(unittest.TestCase):
    """What was entered in gw_sales (collection logs, times) against the photos, every time turned from UTC to IST."""

    def checked(self, request=None, uploads=None, other_logs=(), **kw):
        if uploads is None:
            uploads, self.jev = make_trip()
        request = request or disposal(uploads, **kw)
        with self.jev.client() as client:
            return disposal_check.check(request, files(uploads).get, client, other_logs=list(other_logs))

    def check_of(self, checked, key):
        return next((c for c in checked.analysis["verdict"]["checks"] if c["key"] == key), None)

    def test_a_log_on_another_disposal_is_a_problem(self):
        checked = self.checked(logs=[log(45001, materials=MATERIALS)], other_logs=[(1000, log(45001, materials=MATERIALS))])
        flag = next(f for f in checked.flags if f["code"] == "log_reused")
        self.assertEqual((flag["kind"], flag["severity"]), ("problem found", "high"))
        self.assertEqual(flag["target"], {"type": "collection_log", "id": 45001})
        self.assertEqual(flag["related"], [{"type": "disposal_log", "id": 1000}])
        self.assertIn("is also on disposal 1000. One pickup can't be counted in two disposals.", flag["message"])
        self.assertRegex(flag["message"], r"\(.*, picked up \d\d/\d\d\) is also on")
        self.assertNotIn(") (picked up", flag["message"])

    def test_logs_matching_another_pickup_by_entered_data_are_not_flagged(self):
        # Log weights are placeholders (1 kg) or estimates (4 Nov 2026: 16 wrong "same pickup" flags), so a pickup is
        # only a duplicate by its evidence: the same log id, photo, slip, weighing or vehicle time (the user, 6 Nov).
        checked = self.checked(logs=[log(45001, materials=MATERIALS), log(45002, materials=MATERIALS)],
                               other_logs=[(1000, log(46001, materials=MATERIALS))])
        self.assertEqual([f["code"] for f in checked.flags], [])

    def test_three_shared_logs_are_one_flag_for_the_other_disposal(self):
        mine = [log(45001, materials=MATERIALS), log(45002, materials=MATERIALS), log(45003, materials=MATERIALS)]
        checked = self.checked(logs=mine, other_logs=[(64188, e) for e in mine])
        [flag] = [f for f in checked.flags if f["code"] == "log_reused"]
        self.assertIn("Collection logs 45001 (ABC Hotel, Main Kitchen, picked up 15/10), 45002 (", flag["message"])
        self.assertIn("and 45003 (ABC Hotel, Main Kitchen, picked up 15/10) are also on disposal 64188.", flag["message"])
        self.assertNotIn("target", flag)
        self.assertEqual(flag["related"], [{"type": "disposal_log", "id": 64188}])

    def test_logs_shared_with_two_disposals_are_one_flag_each(self):
        mine = [log(45001), log(45002)]
        checked = self.checked(logs=mine, other_logs=[(1000, mine[0]), (1001, mine[1])])
        flags = [f for f in checked.flags if f["code"] == "log_reused"]
        self.assertEqual([f["related"][0]["id"] for f in flags], [1000, 1001])
        self.assertTrue(all(f["target"]["type"] == "collection_log" for f in flags))

    def test_the_same_pair_in_one_log_and_under_two_logs_is_one_photo_flag(self):
        uploads, self.jev = make_trip(same=(("loading", 0, "loading", 1),))
        request = self.two_logs(uploads)
        shared = next(e for e in request["evidence"] if e["category"] == "collection")
        request["evidence"].append({**shared, "owner": {"type": "collection_log", "id": 45002}})  # also listed twice
        flags = [f for f in self.checked(request, uploads).flags if f["code"] == "log_same_photo"]
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0]["kind"], "problem found")

    def test_pickup_in_utc_is_compared_as_ist(self):
        checked = self.checked(logs=[log()])  # 14 Oct 21:00 UTC = 15 Oct IST, the photos' day
        self.assertEqual(self.check_of(checked, "collection_photo_date")["status"], "pass")
        self.assertEqual(checked.flags, [])

    def test_a_collection_photo_from_another_day_asks_a_person(self):
        checked = self.checked(logs=[log(picked_up="2026-10-13T05:00:00.000Z")])
        c = self.check_of(checked, "collection_photo_date")
        self.assertEqual((c["status"], c["part"]), ("review", "real"))
        self.assertIn("collection-logs/2026/10/15/collection1.jpg was stamped 15/10, but collection log 45001 (ABC Hotel, "
                      "Main Kitchen) was picked up on 13/10", c["detail"])
        self.assertEqual(len(c["images"]), 2, "every photo is checked, not one of them")

    def test_a_photo_of_the_disposal_itself_needs_the_day_of_one_of_its_logs(self):
        uploads, self.jev = make_trip()
        request = disposal(uploads, logs=[log(1, "2026-10-13T05:00:00.000Z"), log(2)])
        for item in request["evidence"]:
            item["owner"] = {"type": "disposal_log", "id": DISPOSAL}
        self.assertEqual(self.check_of(self.checked(request, uploads), "collection_photo_date")["status"], "pass")
        self.jev = make_trip()[1]
        request["reference"]["collectionLogs"] = [log(1, "2026-10-13T05:00:00.000Z")]
        c = self.check_of(self.checked(request, uploads), "collection_photo_date")
        self.assertEqual(c["status"], "review")
        self.assertIn("but the disposal's logs were picked up on 13/10", c["detail"])

    def test_a_log_without_a_photo_is_not_flagged(self):
        # The user, 5 Nov 2026: a missing collection photo isn't flagged, for one log or the whole disposal.
        checked = self.checked(logs=[log(), log(45002, client={"name": "XYZ School"})])
        self.assertFalse([f for f in checked.flags if f.get("target") == {"type": "collection_log", "id": 45002}])
        uploads, self.jev = make_trip(loading=())
        checked = self.checked(uploads=uploads, logs=[log()])
        self.assertEqual((checked.outcome, checked.flags), ("passed", []))

    def two_logs(self, uploads, split=True):
        """Two logs, with the second collection photo under 45002 (or both under 45001 when not `split`)."""
        request = disposal(uploads, logs=[log(45001), log(45002, client={"name": "XYZ School"})])
        if split:
            for item in request["evidence"]:
                if item["s3Key"].endswith(uploads["loading"][1].name):
                    item["owner"] = {"type": "collection_log", "id": 45002}
        return request

    def test_one_photo_listed_under_two_logs_is_a_problem(self):
        uploads, self.jev = make_trip()
        request = disposal(uploads, logs=[log(45001), log(45002, client={"name": "XYZ School"})])
        shared = next(e for e in request["evidence"] if e["category"] == "collection")  # collection1.jpg
        request["evidence"].append({**shared, "owner": {"type": "collection_log", "id": 45002}})
        flag = next(f for f in self.checked(request, uploads).flags if f["code"] == "log_same_photo")
        self.assertEqual((flag["kind"], flag["severity"]), ("problem found", "high"))
        self.assertIn("is the photo of both collection log 45001 (ABC Hotel, Main Kitchen) and 45002 (XYZ School, Main Kitchen)",
                      flag["message"])
        self.assertIn("Each pickup needs its own photo.", flag["message"])

    def test_the_same_file_under_two_keys_in_two_logs_is_a_problem(self):
        uploads, self.jev = make_trip(same=(("loading", 0, "loading", 1),))
        flag = next(f for f in self.checked(self.two_logs(uploads), uploads).flags if f["code"] == "log_same_photo")
        self.assertEqual((flag["kind"], flag["severity"]), ("problem found", "high"))
        self.assertIn("1234 collection photo 1 and 1234 collection photo 2 are the same file", flag["message"])
        self.assertNotIn("target", flag)

    def test_a_look_alike_in_two_logs_asks_a_person(self):
        uploads, self.jev = make_trip()
        uploads["loading"][1] = Upload(uploads["loading"][1].name, coloured(COLOURS[0], 60))
        flag = next(f for f in self.checked(self.two_logs(uploads), uploads).flags if f["code"] == "log_same_photo")
        self.assertEqual(flag["kind"], "please check")
        self.assertIn("look the same. Compare them.", flag["message"])

    def test_the_same_file_twice_in_one_log_asks_a_person(self):
        uploads, self.jev = make_trip(same=(("loading", 0, "loading", 1),))
        checked = self.checked(self.two_logs(uploads, split=False), uploads)
        flag = next(f for f in checked.flags if f["code"] == "log_same_photo")
        self.assertEqual(flag["kind"], "please check")
        self.assertIn("are the same file, both under collection log 45001 (ABC Hotel, Main Kitchen)", flag["message"])
        self.assertEqual(flag["target"], {"type": "collection_log", "id": 45001})

    def test_each_log_with_its_own_photo_passes(self):
        uploads, self.jev = make_trip()
        checked = self.checked(self.two_logs(uploads), uploads)
        self.assertEqual(self.check_of(checked, "log_same_photo")["status"], "pass")
        self.assertFalse([f for f in checked.flags if f["code"] == "log_same_photo"])

    def test_unloading_just_before_new_year_is_not_late(self):
        # End 31 Dec 23:00 IST, unloading stamped 31/12 23:20: by the end time + 1 h 30 min, across New Year.
        images = {"unloading": [{"name": "unloading1.jpg", "readings": [
            {"key": "photo_time", "value": "23:20", "missing": False, "min_confidence": 0.95},
            {"key": "photo_date", "value": "31/12", "missing": False, "min_confidence": 0.95}]}]}
        request = disposal(endTime="2026-12-31T17:30:00.000Z")
        self.assertEqual(entry_checks.too_late(request, images, around=date(2026, 12, 31))["status"], "pass")

    def test_unloading_long_after_the_end_time_asks_a_person(self):
        # End 14 Oct 21:00 UTC = 15 Oct 02:30 IST, so photos by 04:00; the unloading photos are 04:09.
        checked = self.checked(endTime="2026-10-14T21:00:00.000Z")
        c = self.check_of(checked, "unloading_too_late")
        self.assertEqual((c["status"], c["part"]), ("review", "real"))
        self.assertIn("unloading1.jpg was stamped 04:09 on 15/10", c["detail"])
        self.assertIn("after the end time entered, 02:30 on 15/10", c["detail"])
        self.jev = make_trip()[1]
        on_time = self.checked(endTime="2026-10-14T22:40:00.000Z")  # 04:10 IST: photos by 05:40
        self.assertEqual(self.check_of(on_time, "unloading_too_late")["status"], "pass")
        self.jev = make_trip()[1]
        self.assertIsNone(self.check_of(self.checked(), "unloading_too_late"), "no end time: not checked")

    def test_no_pickup_time_is_compared_with_the_arrival(self):
        # collectionDate's time can be hours after the pickup (3 Nov 2026, logs saved at the plant): the user dropped it.
        checked = self.checked(logs=[log(picked_up="2026-10-15T04:00:00.000Z")])  # an hour after the arrival
        self.assertIsNone(self.check_of(checked, "disposal_after_collections"))
        self.assertEqual(checked.flags, [])

    def test_collected_against_weighed_is_recorded_not_flagged(self):
        materials = [{"materialName": "PET", "quantity": 2000, "unit": "kg"}, {"materialName": "Mixed", "quantity": 12, "unit": "bags"}]
        checked = self.checked(logs=[log(materials=materials)])
        self.assertEqual(checked.extractions["collectedVsWeighed"],
                         {"logs": 1, "collectedKg": 2000, "collectedBags": 12, "netEnteredKg": float(QUANTITY), "differenceKg": 570.0})
        self.assertEqual(checked.flags, [])

    def test_the_slip_must_be_from_the_weighbridge_entered(self):
        uploads, self.jev = make_trip(bill=(FIRST, {**FINAL, "weighbridge_entered": ("different", 0.9)}))
        checked = self.checked(disposal(uploads, weighbridge={"id": 5, "name": "ABC Weighbridge"}), uploads)
        self.assertTrue(any(b"ABC Weighbridge" in sent(r) for r in self.jev.requests), "the name entered is sent")
        [flag] = [f for f in checked.flags if f["code"] == "weighbridge_matches"]
        self.assertEqual((flag["kind"], flag["severity"]), ("please check", "recheck"))
        self.assertIn("from another weighbridge than ABC Weighbridge, the one entered", flag["message"])

    def test_no_weighbridge_entered_asks_nothing_about_it(self):
        checked = self.checked()
        self.assertFalse(any(b"weighbridge_entered" in sent(r) for r in self.jev.requests))
        self.assertIsNone(self.check_of(checked, "weighbridge_matches"))

    def point(self, near: bool):
        """A trip whose collection photos are stamped at PLANT (here the collection point) or 38 km away."""
        where = AT_PLANT if near else PULATH
        uploads, self.jev = make_trip(loading=({**LOADING, **where}, {**LOADING, **where}))
        at_point = log(collectionPoint={"name": "Main Kitchen", "location": {"lat": PLANT[0], "lng": PLANT[1]}})
        return self.checked(disposal(uploads, logs=[at_point]), uploads)

    def test_a_collection_photo_at_its_collection_point_passes(self):
        checked = self.point(near=True)
        c = self.check_of(checked, "collection_point")
        self.assertEqual((c["status"], c["part"]), ("pass", "real"))
        lines = [r for r in self.jev.requests if b'"line":{"type":"box"' in sent(r)]
        self.assertEqual(len(lines), 2, "each collection photo's position line is found")

    def test_collection_photos_far_from_the_collection_point_are_a_problem(self):
        [flag] = [f for f in self.point(near=False).flags if f["code"] == "collection_point"]
        self.assertEqual((flag["kind"], flag["severity"]), ("problem found", "high"))
        self.assertEqual(flag["target"], {"type": "collection_log", "id": 45001})
        self.assertIn("from Main Kitchen, the collection point of collection log 45001, but collection must be within "
                      "4 km of it", flag["message"])

    def test_a_collection_point_outside_india_asks_a_person(self):
        uploads, self.jev = make_trip(loading=({**LOADING, **AT_PLANT}, {**LOADING, **AT_PLANT}))
        swapped = log(collectionPoint={"name": "Main Kitchen", "location": {"lat": PLANT[1], "lng": PLANT[0]}})
        checked = self.checked(disposal(uploads, logs=[swapped]), uploads)
        [flag] = [f for f in checked.flags if f["code"] == "collection_point"]
        self.assertEqual((flag["kind"], flag["target"]), ("please check", {"type": "collection_log", "id": 45001}))
        self.assertIn(f"The location in gw_sales of Main Kitchen, the collection point of collection log 45001, "
                      f"{PLANT[1]}, {PLANT[0]}, isn't in India", flag["message"])
        self.assertFalse(any(b'"line":{"type":"box"' in sent(r) for r in self.jev.requests), "nothing to compare with")

    def test_a_facility_outside_india_asks_a_person(self):
        uploads, self.jev = make_trip()
        checked = self.checked(disposal(uploads, facility={"id": 3, "location": {"lat": PLANT[1], "lng": PLANT[0]}}),
                               uploads)
        [flag] = [f for f in checked.flags if f["code"] == "unloading_plant"]
        self.assertEqual((flag["kind"], flag["severity"]), ("please check", "recheck"))
        self.assertIn(f"The facility's location in gw_sales, {PLANT[1]}, {PLANT[0]}, isn't in India", flag["message"])
        self.assertFalse(any(b'"line":{"type":"box"' in sent(r) for r in self.jev.requests), "nothing to compare with")
        self.assertFalse(checked.standard["plant"])

    def test_without_a_known_collection_point_no_position_is_read(self):
        checked = self.checked(logs=[log()])
        self.assertIsNone(self.check_of(checked, "collection_point"))
        self.assertFalse(any(b'"line":{"type":"box"' in sent(r) for r in self.jev.requests))

    def test_without_logs_or_times_nothing_new_is_checked(self):
        keys = {c["key"] for c in self.checked().analysis["verdict"]["checks"]}
        self.assertFalse(keys & {"collection_photo_date", "unloading_too_late"})


class CompareTests(unittest.TestCase):
    """A disposal compared with its vehicle's other disposals."""

    def checked(self, uploads, jev, others=(), disposal_id=DISPOSAL, **claims):
        with jev.client() as client:
            return disposal_check.check(disposal(uploads, disposal_id=disposal_id, **claims), files(uploads).get, client,
                                 others=list(others))

    def first(self):
        """Disposal 1000, already checked: make_trip's images, weighed 02:00-03:30."""
        uploads, jev = make_trip()
        return compare_disposals.Other(1000, self.checked(uploads, jev, disposal_id=1000).analysis)

    def flag(self, checked, code):
        return next((f for f in checked.flags if f["code"] == code), None)

    def test_same_images_again_are_a_problem_naming_the_other_disposal(self):
        uploads, jev = other_trip(colours=0, bill=LATER)  # make_trip's files, renamed
        checked = self.checked(uploads, jev, [self.first()])
        flag = self.flag(checked, "reused")
        self.assertEqual((flag["kind"], flag["severity"]), ("problem found", "high"))
        self.assertEqual(flag["related"], [{"type": "disposal_log", "id": 1000}])
        self.assertIn("1234 collection photo 1 is the same file as collection photo 1 in disposal 1000", flag["message"])
        self.assertIn("1234 weigh bill 2 is the same file as weigh bill 2 in disposal 1000", flag["message"])
        self.assertNotIn(".jpg", flag["message"], "images are named for people, not by s3Key")
        self.assertIn("collection-logs/2026/10/15/b-collection1.jpg", flag["evidence"], "evidence keeps the s3Keys")
        self.assertIsNone(self.flag(checked, "same_moment"), "copies are reused's, not also the same moment")

    def test_look_alike_asks_a_person(self):
        uploads, jev = other_trip(colours=0, quality=60, bill=LATER)
        flag = self.flag(self.checked(uploads, jev, [self.first()]), "reused")
        self.assertEqual((flag["kind"], flag["severity"]), ("please check", "recheck"))
        self.assertIn("looks the same as", flag["message"])

    def test_photos_borrowed_by_a_trip_of_another_quantity_are_a_problem(self):
        # As 173186 on 23 Oct 2026: its collection photos were 172929's unloading photos; 1870 kg against 1990 kg.
        first, _ = make_trip()
        uploads, jev = other_trip(unloading={**UNLOADING, **stamp("07:30")}, bill=LATER)
        uploads["loading"] = [Upload(f"b-collection{i + 1}.jpg", u.data) for i, u in enumerate(first["unloading"])]
        jev.by_colour.update(make_trip()[1].by_colour)
        checked = self.checked(uploads, jev, [self.first()], quantity=999)
        flag = self.flag(checked, "reused")
        self.assertEqual((flag["kind"], flag["severity"]), ("problem found", "high"))
        self.assertIn("1234 collection photo 1 is the same file as unloading photo 1 in disposal 1000", flag["message"])

    def test_photos_at_the_same_moment_ask_a_person(self):
        # Disposal 1000's last unloading photo is 04:09; this trip's collection photos are 04:11, its unloading 05:30.
        # The trips don't overlap (by more than the clock slack), but 04:11 and 04:09 look like one moment.
        uploads, jev = other_trip(loading={**LOADING, **stamp("04:11")}, unloading={**UNLOADING, **stamp("05:30")}, bill=LATER)
        checked = self.checked(uploads, jev, [self.first()])
        flag = self.flag(checked, "same_moment")
        self.assertEqual((flag["kind"], flag["severity"]), ("please check", "recheck"))
        self.assertEqual(flag["related"], [{"type": "disposal_log", "id": 1000}])
        self.assertIn("1234 collection photo 1 was stamped 04:11 on 15/10, unloading photo 2 in disposal 1000 04:09 on 15/10",
                      flag["message"])
        self.assertIsNone(self.flag(checked, "overlap"))
        self.assertIsNone(self.flag(checked, "reused"))

    def test_more_than_five_minutes_apart_is_not_the_same_moment(self):
        uploads, jev = other_trip(loading={**LOADING, **stamp("04:15")}, unloading={**UNLOADING, **stamp("05:30")}, bill=LATER)
        checked = self.checked(uploads, jev, [self.first()])
        self.assertIsNone(self.flag(checked, "same_moment"), "04:15 is 6 min after 04:09")
        self.assertIsNone(self.flag(checked, "overlap"))

    def test_another_trip_at_the_same_time_is_a_problem_whatever_the_quantity(self):
        uploads, jev = other_trip(bill=(FIRST, {**FINAL, **at("gross", "03:40")}))  # other photos, stamped 02:40-04:09 as disposal 1000's
        checked = self.checked(uploads, jev, [self.first()], quantity=999)
        flag = self.flag(checked, "overlap")
        self.assertEqual((flag["kind"], flag["severity"]), ("problem found", "high"))
        self.assertIn("KL05AF9341's photos for this disposal were taken 02:40-04:09 on 15/10, overlapping disposal "
                      "1000's 02:40-04:09 on 15/10", flag["message"])
        self.assertEqual(flag["related"], [{"type": "disposal_log", "id": 1000}])

    def test_an_unsure_overlap_is_named_beside_a_sure_one(self):
        # Disposal 1001 ran 05:20-06:40 by its sure stamps; an unloading stamp read as 03:00, 30% sure, would put it
        # on this trip (02:40-04:09), which surely overlaps disposal 1000's.
        unsure = {**UNLOADING, **stamp("03:00", confidence=0.3)}
        uploads, jev = other_trip("c-", 0, loading={**LOADING, **stamp("05:20")}, quality=60,
                                  unloading=({**UNLOADING, **stamp("06:40")}, unsure), bill=(FIRST, {**FINAL, **at("gross", "05:00")}))
        doubtful = compare_disposals.Other(1001, self.checked(uploads, jev, disposal_id=1001).analysis)
        uploads, jev = other_trip(bill=(FIRST, {**FINAL, **at("gross", "03:40")}))
        flag = self.flag(self.checked(uploads, jev, [self.first(), doubtful], quantity=999), "overlap")
        self.assertEqual(flag["kind"], "problem found")
        self.assertIn("It may also overlap disposal 1001, by stamps Jev wasn't sure of", flag["message"])
        self.assertEqual(flag["related"], [{"type": "disposal_log", "id": 1000}, {"type": "disposal_log", "id": 1001}])

    def test_a_pair_is_flagged_once_by_the_most_specific_check(self):
        uploads, jev = other_trip(bill=(FIRST, {**FINAL, **at("gross", "03:40")}))  # the same moments too, but overlap already names disposal 1000
        checked = self.checked(uploads, jev, [self.first()])
        self.assertIsNotNone(self.flag(checked, "overlap"))
        self.assertIsNone(self.flag(checked, "same_moment"))
        uploads, jev = other_trip(colours=0)  # copies: reused only
        checked = self.checked(uploads, jev, [self.first()])
        self.assertEqual([f["code"] for f in checked.flags if f.get("related")], ["reused"])

    def test_several_trips_a_day_on_one_morning_tare_are_not_an_overlap(self):
        # As KL57AD9265 on 19 Oct 2026: one tare weighed in the morning (02:00 here) printed on every slip. By the
        # weighings this trip (02:00-06:30) would contain disposal 1000's (02:00-03:30); by the photos it comes after.
        morning = (FIRST, {**FINAL, **at("gross", "06:30")})
        uploads, jev = other_trip(loading={**LOADING, **stamp("05:20")}, unloading={**UNLOADING, **stamp("06:40")}, bill=morning)
        checked = self.checked(uploads, jev, [self.first()], quantity=999)
        self.assertEqual(next(c for c in checked.analysis["verdict"]["checks"] if c["key"] == "overlap")["status"], "pass")

    def test_overlap_on_an_unsure_stamp_asks_a_person(self):
        # This trip ran 05:20-06:40 by its sure stamps; one unloading stamp read as 03:00, only 30% sure, would put it
        # on disposal 1000's trip (02:40-04:09).
        unsure = {**UNLOADING, **stamp("03:00", confidence=0.3)}
        uploads, jev = other_trip(loading={**LOADING, **stamp("05:20")}, unloading=({**UNLOADING, **stamp("06:40")}, unsure),
                                  bill=(FIRST, {**FINAL, **at("gross", "05:00")}))
        flag = self.flag(self.checked(uploads, jev, [self.first()], quantity=999), "overlap")
        self.assertEqual(flag["kind"], "please check")
        self.assertIn("1234 unloading photo 2 was read as 03:00", flag["message"])
        self.assertIn("30% sure", flag["message"])

    def test_without_collection_photos_the_trip_is_timed_from_the_gross_weighing(self):
        uploads, jev = make_trip(loading=())
        first = compare_disposals.Other(1000, self.checked(uploads, jev, disposal_id=1000).analysis)  # 03:30-04:09
        uploads, jev = other_trip(unloading={**UNLOADING, **stamp("04:30")}, bill=(FIRST, {**FINAL, **at("gross", "03:50")}))
        uploads["loading"] = []
        flag = self.flag(self.checked(uploads, jev, [first], quantity=999), "overlap")
        self.assertEqual(flag["kind"], "problem found")
        self.assertIn("KL05AF9341's gross weighing and unloading photos for this disposal were at 03:50-04:30 on 15/10, "
                      "overlapping disposal 1000's 03:30-04:09 on 15/10", flag["message"])

    def test_an_unsure_gross_weighing_that_starts_the_trip_is_named_to_check(self):
        uploads, jev = make_trip(loading=())
        first = compare_disposals.Other(1000, self.checked(uploads, jev, disposal_id=1000).analysis)
        uploads, jev = other_trip(unloading={**UNLOADING, **stamp("04:30")},
                                  bill=(FIRST, {**FINAL, **at("gross", "03:50", confidence=0.5)}))
        uploads["loading"] = []
        flag = self.flag(self.checked(uploads, jev, [first], quantity=999), "overlap")
        self.assertEqual(flag["kind"], "please check")
        self.assertIn("1234 weigh bill 2 was read as 03:50 on 15/10, but only 50% sure", flag["message"])

    def test_trips_without_both_kinds_of_photo_are_not_timed(self):
        uploads, jev = other_trip()
        uploads["unloading"] = []
        checked = self.checked(uploads, jev, [self.first()], quantity=999)
        self.assertNotIn("overlap", {c["key"] for c in checked.analysis["verdict"]["checks"]})

    def test_a_new_photo_of_the_same_slip_is_the_same_weighing(self):
        uploads, jev = other_trip(loading={**LOADING, **stamp("05:20")}, unloading={**UNLOADING, **stamp("06:40")})
        # other photos, the same bill readings as disposal 1000: gross 03:30 on 15/10
        flag = self.flag(self.checked(uploads, jev, [self.first()]), "same_weighing")
        self.assertEqual((flag["kind"], flag["severity"]), ("problem found", "high"))
        self.assertEqual(flag["related"], [{"type": "disposal_log", "id": 1000}])
        self.assertIn("1234 weigh bill 2 shows the gross weighing at 03:30 on 15/10, the same as disposal 1000's weigh bill 2",
                      flag["message"])
        self.assertIn("One weighing can't be proof for two disposals.", flag["message"])

    def test_the_same_net_weight_is_named_when_both_quantities_match(self):
        first = compare_disposals.Other(1000, self.first().analysis, f"{QUANTITY} kg")
        uploads, jev = other_trip(loading={**LOADING, **stamp("05:20")}, unloading={**UNLOADING, **stamp("06:40")})
        flag = self.flag(self.checked(uploads, jev, [first]), "same_weighing")
        self.assertIn(f"and the same net weight, {QUANTITY} kg", flag["message"])

    def test_a_quantity_not_in_kg_still_finds_the_same_weighing_without_a_net_weight(self):
        # net_weight is only asked of a quantity in kg, so these slips have no answer for it
        first = compare_disposals.Other(1000, self.first().analysis, "2.5 tonnes")
        uploads, jev = other_trip(loading={**LOADING, **stamp("05:20")}, unloading={**UNLOADING, **stamp("06:40")})
        checked = self.checked(uploads, jev, [first], quantity=2.5, unit="tonnes")
        self.assertEqual(checked.outcome, "flagged")
        flag = self.flag(checked, "same_weighing")
        self.assertEqual(flag["kind"], "problem found")
        self.assertNotIn("net weight", flag["message"])

    def test_an_unsure_gross_time_asks_a_person(self):
        unsure = (FIRST, {**FINAL, **at("gross", "03:31", confidence=0.4)})
        uploads, jev = other_trip(loading={**LOADING, **stamp("05:20")}, unloading={**UNLOADING, **stamp("06:40")}, bill=unsure)
        flag = self.flag(self.checked(uploads, jev, [self.first()]), "same_weighing")
        self.assertEqual(flag["kind"], "please check")
        self.assertIn("40% sure", flag["message"])

    def test_three_minutes_apart_is_another_weighing(self):
        uploads, jev = other_trip(loading={**LOADING, **stamp("05:20")}, unloading={**UNLOADING, **stamp("06:40")},
                                  bill=(FIRST, {**FINAL, **at("gross", "03:33")}))
        checked = self.checked(uploads, jev, [self.first()])
        self.assertEqual(next(c for c in checked.analysis["verdict"]["checks"] if c["key"] == "same_weighing")["status"], "pass")

    def test_one_morning_tare_is_not_the_same_weighing(self):
        morning = (FIRST, {**FINAL, **at("gross", "06:30")})  # tare 02:00 as disposal 1000's, another gross
        uploads, jev = other_trip(loading={**LOADING, **stamp("05:20")}, unloading={**UNLOADING, **stamp("06:40")}, bill=morning)
        self.assertIsNone(self.flag(self.checked(uploads, jev, [self.first()]), "same_weighing"))

    def test_one_widely_shared_file_names_five_disposals_and_counts_the_rest(self):
        uploads, jev = make_trip()
        name = "collection-logs/2026/10/15/collection1.jpg"
        md5 = proof_check.fingerprints(files(uploads)[name])["md5"]
        copies = [database.Copy(name, "loading", md5, 900 + i, "KL07BC1234", date(2026, 9, 1 + i)) for i in range(7)]
        with jev.client() as client:
            checked = disposal_check.check(disposal(uploads), files(uploads).get, client, history=lambda rows: copies)
        flag = self.flag(checked, "reused")
        self.assertEqual([r["id"] for r in flag["related"]], [902, 903, 904, 905, 906], "the latest five by trip day")
        self.assertIn("and 2 more disposals", flag["message"])
        self.assertNotIn("disposal 900 ", flag["message"])
        self.assertNotIn("disposal 901 ", flag["message"])

    def test_two_slips_are_named_when_a_doubtful_time_asks_a_person(self):
        unsure = (FIRST, {**FINAL, **at("gross", "03:31", confidence=0.4)})
        uploads, jev = other_trip(loading={**LOADING, **stamp("05:20")}, unloading={**UNLOADING, **stamp("06:40")}, bill=unsure)
        flag = self.flag(self.checked(uploads, jev, [self.first()]), "same_weighing")
        self.assertIn("disposal/2026/10/15/b-weighbill2.jpg", flag["evidence"])
        self.assertIn("disposal/2026/10/15/weighbill2.jpg", flag["evidence"])

    def test_a_sure_same_weighing_is_a_problem_naming_a_doubtful_one_beside_it(self):
        unsure = (FIRST, {**FINAL, **at("gross", "03:31", confidence=0.4)})
        uploads, jev = other_trip("c-", 0, loading={**LOADING, **stamp("05:20")}, quality=60,
                                  unloading={**UNLOADING, **stamp("06:40")}, bill=unsure)
        doubtful = compare_disposals.Other(1001, self.checked(uploads, jev, disposal_id=1001).analysis)
        uploads, jev = other_trip(loading={**LOADING, **stamp("05:20")}, unloading={**UNLOADING, **stamp("06:40")})
        flag = self.flag(self.checked(uploads, jev, [self.first(), doubtful]), "same_weighing")
        self.assertEqual(flag["kind"], "problem found")
        self.assertIn("It may also be the same weighing as disposal 1001, by times Jev wasn't sure of", flag["message"])
        self.assertEqual(flag["related"], [{"type": "disposal_log", "id": 1000}, {"type": "disposal_log", "id": 1001}])

    def test_alone_every_comparison_passes(self):
        uploads, jev = make_trip()
        checks = {c["key"]: c["status"] for c in self.checked(uploads, jev).analysis["verdict"]["checks"]}
        self.assertEqual({k: checks[k] for k in ("same_file", "reused", "same_weighing", "same_moment", "overlap")},
                         dict.fromkeys(("same_file", "reused", "same_weighing", "same_moment", "overlap"), "pass"))

    def test_one_file_as_collection_and_unloading_photo_is_a_problem(self):
        uploads, jev = make_trip(same=(("loading", 0, "unloading", 0),))
        flag = self.flag(self.checked(uploads, jev), "same_file")
        self.assertEqual((flag["kind"], flag["severity"]), ("problem found", "high"))
        self.assertIn("1234 collection photo 1 and 1234 unloading photo 1", flag["message"])

    def test_without_a_vehicle_number_only_the_disposal_itself_is_checked(self):
        uploads, jev = other_trip(colours=0, bill=LATER)
        checked = self.checked(uploads, jev, [self.first()], vehicle={"vehicleNumber": ""})
        keys = {c["key"] for c in checked.analysis["verdict"]["checks"]}
        self.assertIn("same_file", keys)
        self.assertFalse(keys & {"same_moment", "overlap", "same_weighing"})
        c = next(c for c in checked.analysis["verdict"]["checks"] if c["key"] == "not_compared")
        self.assertEqual((c["status"], c["part"]), ("review", "real"))

    def copy(self, name="collection-logs/2026/10/15/collection1.jpg", disposal_id=900, vehicle="KL07BC1234", day=date(2026, 9, 1)):
        uploads, _ = make_trip()
        md5 = proof_check.fingerprints(files(uploads)[name])["md5"]
        return database.Copy(name, "loading", md5, disposal_id, vehicle, day)

    def test_a_copy_from_an_older_disposal_of_another_vehicle_is_a_problem(self):
        uploads, jev = make_trip()
        with jev.client() as client:
            checked = disposal_check.check(disposal(uploads), files(uploads).get, client, history=lambda rows: [self.copy()])
        flag = self.flag(checked, "reused")
        self.assertEqual((flag["kind"], flag["severity"]), ("problem found", "high"))
        self.assertIn("1234 collection photo 1 is the same file as collection photo in disposal 900 (KL07BC1234, 01/09)",
                      flag["message"], "named by its kind when that disposal's evidence isn't known")
        self.assertEqual(flag["related"], [{"type": "disposal_log", "id": 900}])

    def test_a_copy_is_named_by_its_own_disposals_evidence(self):
        uploads, jev = make_trip()
        copy = self.copy()
        copy.evidence = disposal(uploads, disposal_id=900)["evidence"][::-1]  # listed last of its two there
        with jev.client() as client:
            checked = disposal_check.check(disposal(uploads), files(uploads).get, client, history=lambda rows: [copy])
        self.assertIn("is the same file as collection photo 2 in disposal 900", self.flag(checked, "reused")["message"])

    def test_prints_are_handed_over_before_jev_is_asked(self):
        uploads, jev = make_trip()
        seen = []

        def history(rows):
            seen.append((len(jev.requests), sorted(name for _, name, _, _ in rows)))
            return []
        with jev.client() as client:
            disposal_check.check(disposal(uploads), files(uploads).get, client, history=history)
        self.assertEqual(seen, [(0, sorted(files(uploads)))])

    def test_an_unavailable_image_has_no_print(self):
        uploads, jev = make_trip()
        stored = files(uploads)
        del stored["disposal/2026/10/15/unloading2.jpg"]
        rows = []
        with jev.client() as client:
            disposal_check.check(disposal(uploads), stored.get, client, history=lambda r: rows.extend(r) or [])
        self.assertNotIn("disposal/2026/10/15/unloading2.jpg", {name for _, name, _, _ in rows})

    def test_without_a_vehicle_number_copies_are_still_found(self):
        uploads, jev = make_trip()
        with jev.client() as client:
            checked = disposal_check.check(disposal(uploads, vehicle={"vehicleNumber": ""}), files(uploads).get, client,
                                           history=lambda rows: [self.copy()])
        keys = {c["key"]: c["status"] for c in checked.analysis["verdict"]["checks"]}
        self.assertEqual((keys["reused"], keys["not_compared"]), ("fail", "review"))


class FlagTests(unittest.TestCase):
    def test_one_flag_per_cause_led_by_a_problem(self):
        checks = [proof_check.check("times", "weigh_gap", "Gap", "review", "Times unsure.", 0.6, ["a"], "weigh_times"),
                  proof_check.check("times", "weigh_order", "Order", "fail", "Order wrong.", 0.9, ["b"], "weigh_times"),
                  proof_check.check("bill", "material", "Material", "review", "Material unsure.", 0.5),
                  proof_check.check("bill", "bill_found", "Final slip", "pass", "Fine.")]
        flags = disposal_check.flags_of(checks)
        self.assertEqual(len(flags), 2)
        first = flags[0]
        self.assertEqual((first["code"], first["kind"], first["severity"]), ("weigh_order", "problem found", "high"))
        self.assertEqual(first["message"], "Order wrong. It also affects: Gap.")
        self.assertEqual((first["evidence"], first["checks"], first["confidence"]), (["b", "a"], ["weigh_order", "weigh_gap"], 0.6))
        self.assertEqual((flags[1]["code"], flags[1]["severity"]), ("material", "info"))

    def test_severity_by_part_and_status(self):
        def severity(key, status):
            return disposal_check.flags_of([proof_check.check("x", key, "x", status, "x")])[0]["severity"]
        self.assertEqual(severity("bill_number", "fail"), "high")
        self.assertEqual(severity("bill_number", "review"), "recheck")
        self.assertEqual(severity("net_weight", "fail"), "recheck")
        self.assertEqual(severity("material", "fail"), "recheck")
        self.assertEqual(severity("material", "review"), "info")


class LogTests(unittest.TestCase):
    def record(self, message: str, level=logging.INFO) -> logging.LogRecord:
        return logging.LogRecord("verifier", level, __file__, 1, message, (), None)

    def test_json_lines_carry_the_request_and_alerts(self):
        rid = "0b6f9a52-3c1e-4d7a-9e43-6a1f2d8c7b10"
        line = json.loads(logs.JsonLines().format(self.record(f"{rid}: passed with 0 flag(s)")))
        self.assertEqual((line["requestId"], line["level"], line["logger"]), (rid, "INFO", "verifier"))
        self.assertNotIn("alert", line)
        alert = json.loads(logs.JsonLines().format(self.record(f"ALERT {rid}: gw_sales refused the result", logging.ERROR)))
        self.assertEqual((alert["alert"], alert["requestId"]), (True, rid))
        self.assertTrue(alert["time"].endswith("Z"))

    def test_plain_lines_without_a_request(self):
        line = json.loads(logs.JsonLines().format(self.record("refused a request (None): bad or expired signature")))
        self.assertNotIn("requestId", line)


class BackoffTests(unittest.TestCase):
    def test_send_retries_for_about_24_hours(self):
        job = mock.Mock()
        waits, attempts = [], 0
        while True:
            job.attempts = attempts
            decision = tasks.SEND_RETRY.get_retry_decision(exception=tasks.Unavailable(), job=job)
            if decision is None:
                break
            waits.append(tasks.SEND_RETRY.wait(attempts))
            attempts += 1
        self.assertEqual(waits[:3], [30, 60, 120])
        self.assertEqual(max(waits), 3600)
        self.assertTrue(23 * 3600 < sum(waits) <= 24 * 3600, sum(waits))

    def test_checking_retries_only_what_can_change(self):
        job = mock.Mock(attempts=0)
        self.assertTrue(tasks.VERIFY_RETRY.gives_up(KeyError("weigh_bill"), job), "a bug fails at once")
        for attempts in range(5):
            job.attempts = attempts
            self.assertFalse(tasks.VERIFY_RETRY.gives_up(disposal_check.JevUnavailable(), job), attempts)
        job.attempts = 5
        self.assertTrue(tasks.VERIFY_RETRY.gives_up(disposal_check.JevUnavailable(), job), "6 tries in all")

    def test_only_unavailable_is_retried_when_sending(self):
        job = mock.Mock(attempts=0)
        self.assertIsNone(tasks.SEND_RETRY.get_retry_decision(exception=ValueError(), job=job))


ADMIN = DB.rsplit("/", 1)[0] + "/verifier"  # the dev database, to create the test ones from


def database_ready() -> bool:
    """Whether the dev Postgres is up; creates the test database on it the first time."""
    try:
        with psycopg.connect(ADMIN, autocommit=True, connect_timeout=2) as conn:
            if not conn.execute("SELECT 1 FROM pg_database WHERE datname = 'verifier_test'").fetchone():
                conn.execute("CREATE DATABASE verifier_test")
    except psycopg.OperationalError:
        return False
    return True


def empty_database(name: str) -> str:
    """A new, empty database on the dev Postgres (dropped first if there); its URL."""
    with psycopg.connect(ADMIN, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        conn.execute(f"CREATE DATABASE {name}")
    return DB.rsplit("/", 1)[0] + "/" + name


@unittest.skipUnless(database_ready(), f"no Postgres at {DB}")
class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.settings = Settings(**{**SETTINGS.__dict__, "database_url": empty_database("verifier_test_migrate")})

    async def migrate(self) -> list[str]:
        async with tasks.running(self.settings) as svc:
            return await migrate.apply(svc.pool)

    async def recorded(self) -> set[str]:
        async with await psycopg.AsyncConnection.connect(self.settings.database_url) as conn:
            cur = await conn.execute("SELECT name FROM verifier_migration")
            return {row[0] for row in await cur.fetchall()}

    async def test_new_database_then_nothing_twice(self):
        applied = await self.migrate()
        self.assertTrue(applied[0].startswith("procrastinate 3."), applied)
        self.assertIn("verifier/001_requests_and_results.sql", applied)
        self.assertIn("verifier/004_jev_answers.sql", applied)
        self.assertIn("verifier/008_image_print.sql", applied)
        recorded = await self.recorded()
        self.assertTrue({"procrastinate/" + p.name for p in migrate.procrastinate_migrations()} <= recorded)
        self.assertEqual(await self.migrate(), [])

    async def test_times_are_shown_in_ist_after_migrating(self):
        await self.migrate()
        async with await psycopg.AsyncConnection.connect(self.settings.database_url) as conn:
            cur = await conn.execute("SELECT current_setting('TimeZone'), to_char(now(), 'OF')")
            self.assertEqual(await cur.fetchone(), ("Asia/Kolkata", "+05:30"))

    async def test_new_migration_is_applied_once(self):
        await self.migrate()
        with tempfile.TemporaryDirectory() as folder:
            extra = Path(folder) / "002_extra.sql"
            extra.write_text("ALTER TABLE verification_request ADD COLUMN extra text;")
            with mock.patch.object(migrate, "our_migrations", lambda: [*sorted(migrate.OURS.glob("0*.sql")), extra]):
                self.assertEqual(await self.migrate(), ["verifier/002_extra.sql"])
                self.assertEqual(await self.migrate(), [])
        self.assertIn("verifier/002_extra.sql", await self.recorded())

    async def test_failed_migration_is_not_recorded(self):
        await self.migrate()
        with tempfile.TemporaryDirectory() as folder:
            bad = Path(folder) / "002_bad.sql"
            bad.write_text("CREATE TABLE half_done (id int); SELECT no_such_function();")
            with mock.patch.object(migrate, "our_migrations", lambda: [*sorted(migrate.OURS.glob("0*.sql")), bad]):
                with self.assertRaises(psycopg.errors.UndefinedFunction):
                    await self.migrate()
        self.assertNotIn("verifier/002_bad.sql", await self.recorded())
        async with await psycopg.AsyncConnection.connect(self.settings.database_url) as conn:
            cur = await conn.execute("SELECT to_regclass('half_done')")
            self.assertIsNone((await cur.fetchone())[0], "rolled back whole")

    async def test_queue_tables_not_made_by_migrate_are_refused(self):
        async with await psycopg.AsyncConnection.connect(self.settings.database_url, autocommit=True) as conn:
            await conn.execute(tasks.app.schema_manager.get_schema())
        with self.assertRaises(SystemExit):
            await self.migrate()


@unittest.skipUnless(database_ready(), f"no Postgres at {DB}")
class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.gw = FakeGwSales()
        self.jev = FakeJev(by_colour={})
        env = mock.patch.dict(os.environ, {"AWS_DEFAULT_REGION": "ap-south-1", "AWS_ACCESS_KEY_ID": "testing",
                                           "AWS_SECRET_ACCESS_KEY": "testing"})
        env.start()
        self.addCleanup(env.stop)
        aws = mock_aws()
        aws.start()
        self.addCleanup(aws.stop)
        boto3.client("s3").create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "ap-south-1"})
        self.running = tasks.running(SETTINGS, httpx.MockTransport(lambda r: self.gw(r)), self.jev.client())
        self.svc = await self.running.__aenter__()
        await migrate.apply(self.svc.pool)
        async with self.svc.pool.connection() as conn:
            await conn.execute("TRUNCATE verification_result, verification_request, procrastinate_jobs, "
                               "procrastinate_periodic_defers, procrastinate_workers CASCADE")
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://verifier")

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.running.__aexit__(None, None, None)

    async def post(self, request: dict, **kw) -> httpx.Response:
        body, headers = signed(request, **kw)
        return await self.client.post("/v1/verifications", content=body, headers=headers)

    async def work(self):
        """Run every job that is due, then stop."""
        await tasks.app.run_worker_async(wait=False, install_signal_handlers=False, listen_notify=False)

    async def sql(self, query: str, *args) -> list[tuple]:
        async with self.svc.pool.connection() as conn:
            cur = await conn.execute(query, args)
            return await cur.fetchall() if cur.description else []

    def store_trip(self, **kw) -> dict[str, list[Upload]]:
        """A trip's images put in S3, with the fake Jev knowing their answers (test_offline.make_trip)."""
        uploads, jev = make_trip(**kw)
        self.jev.by_colour.update(jev.by_colour)
        s3 = boto3.client("s3")
        for key, data in files(uploads).items():
            s3.put_object(Bucket=BUCKET, Key=key, Body=data)
        return uploads

    async def test_the_migration_fills_prints_from_saved_results(self):
        # A result saved before 008: its analysis holds the prints. Run 008's backfill SQL again and find them.
        uploads = self.store_trip()
        await self.post(disposal(uploads, disposal_id=1000))
        await self.work()
        await self.sql("TRUNCATE image_print")
        async with self.svc.pool.connection() as conn:
            await conn.execute((migrate.OURS / "008_image_print.sql").read_text().split("-- backfill\n", 1)[1])
        [(n,)] = await self.sql("SELECT count(*) FROM image_print")
        self.assertEqual(n, 6)

    async def test_a_plain_image_is_not_looked_up_in_all_history(self):
        a, b = disposal(disposal_id=1000), disposal(disposal_id=1001)
        await database.save_request(self.svc.pool, a)
        await database.save_prints(self.svc.pool, a["requestId"],
                                   [("loading", "plain.jpg", "m0", "00" * 8), ("loading", "k1.jpg", "m1", "0f" * 8)])
        await database.save_request(self.svc.pool, b)
        history = database.PrintHistory(self.svc.pool, asyncio.get_running_loop(), b["requestId"], 1001)
        found = await history.find([("loading", "p.jpg", "m0", "00" * 8), ("loading", "k.jpg", "m1", "0f" * 8)])
        self.assertEqual([c.md5 for c in found], ["m1"])
        [(n,)] = await self.sql("SELECT count(*) FROM image_print WHERE request_id = %s", b["requestId"])
        self.assertEqual(n, 2, "both are still saved")
        self.assertEqual(await database.copies(self.svc.pool, 1001, []), [])

    async def test_a_look_alike_in_an_older_disposal_is_not_flagged(self):
        # Another vehicle's disposal, two days earlier, whose images are re-saved look-alikes (not exact copies).
        uploads, jev = other_trip(colours=0, quality=60)
        self.jev.by_colour.update(jev.by_colour)
        s3 = boto3.client("s3")
        for key, data in files(uploads).items():
            s3.put_object(Bucket=BUCKET, Key=key, Body=data)
        await self.post(disposal(uploads, disposal_id=1000, vehicle={"id": 13, "vehicleNumber": "kl 07 zz 9999"}))
        await self.work()
        await self.sql("UPDATE verification_request SET trip_day = trip_day - 2 WHERE disposal_id = 1000")
        again = disposal(self.store_trip(), disposal_id=1001)
        await self.post(again)
        await self.work()
        self.assertNotIn("reused", {f["code"] for f in self.gw.stored[again["requestId"]]["flags"]})

    async def test_other_logs_finds_a_log_id_on_any_date_and_others_within_a_day(self):
        day = date(2026, 10, 15)
        for disposal_id, log_id, back in ((1000, 45001, 60), (1001, 46001, 1), (1002, 47001, 3), (DISPOSAL, 45001, 0)):
            request = disposal(disposal_id=disposal_id, logs=[log(log_id)])
            await database.save_request(self.svc.pool, request)
            await database.note_disposal(self.svc.pool, request["requestId"], None, None, day - timedelta(days=back))
        found = await database.other_logs(self.svc.pool, DISPOSAL, [45001], day)
        self.assertEqual(sorted((i, e["id"]) for i, e in found), [(1000, 45001), (1001, 46001)])

    async def test_a_log_on_two_disposals_is_flagged_end_to_end(self):
        uploads = self.store_trip()
        await self.post(disposal(uploads, disposal_id=1000, logs=[log(45001)]))
        await self.work()
        second = disposal(uploads, disposal_id=1001, logs=[log(45001)], vehicle={"id": 13, "vehicleNumber": "kl 07 zz 9999"})
        await self.post(second)
        await self.work()
        [(result,)] = await self.sql("SELECT body FROM verification_result WHERE request_id = %s", second["requestId"])
        self.assertIn("log_reused", {f["code"] for f in result["flags"]})

    async def test_copies_are_found_in_any_vehicles_latest_request_never_its_own(self):
        a = disposal(disposal_id=1000)
        await database.save_request(self.svc.pool, a)
        await database.save_prints(self.svc.pool, a["requestId"], [("loading", "k1.jpg", "m1", "0f" * 8)])
        await database.save_prints(self.svc.pool, a["requestId"], [("loading", "k1.jpg", "m1", "0f" * 8)])  # a retry
        found = await database.copies(self.svc.pool, 1001, ["m1", "m2"])
        self.assertEqual([(c.name, c.disposal_id) for c in found], [("k1.jpg", 1000)])
        self.assertEqual(await database.copies(self.svc.pool, 1000, ["m1"]), [], "never its own")
        edit = disposal(disposal_id=1000, reason="edited", data_version="sha256:bbb")
        await database.save_request(self.svc.pool, edit)
        await database.save_prints(self.svc.pool, edit["requestId"], [("loading", "k2.jpg", "m2", "0f" * 8)])
        self.assertEqual([c.name for c in await database.copies(self.svc.pool, 1001, ["m1", "m2"])], ["k2.jpg"],
                         "an image dropped in an edit is forgotten")

    async def make_due(self):
        """Bring retries forward to now."""
        await self.sql("UPDATE procrastinate_jobs SET scheduled_at = now() WHERE status = 'todo'")

    # gw_sales' end-to-end cases

    async def test_new_disposal_gets_a_signed_result(self):
        request = disposal(self.store_trip())
        response = await self.post(request)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {"accepted": True, "duplicate": False})
        await self.work()
        self.assertEqual(len(self.gw.calls), 1)
        call = self.gw.calls[0]
        self.assertEqual(str(call.url), request["callbackUrl"])
        result = self.gw.stored[request["requestId"]]
        self.assertEqual(result["subject"], {"type": "disposal_log", "id": DISPOSAL, "dataVersion": "sha256:aaa"})
        self.assertEqual((result["outcome"], result["flags"]), ("passed", []))
        self.assertEqual(result["verifier"]["name"], tasks.NAME)
        self.assertEqual(result["standard"]["models"], {"questions": "jev-1.14.0", "box": "jev-1.14.0"})
        self.assertEqual(result["extractions"]["parts"], {"real": "pass", "docs": "pass"})
        row = await database.result(self.svc.pool, request["requestId"])
        self.assertIsNotNone(row["sent_at"])
        self.assertEqual((row["attempts"], row["last_status"]), (1, 200))
        self.assertGreater(row["cost_usd"], 0)
        self.assertEqual(row["analysis"]["verdict"]["status"], "pass", "proof_check's whole result is kept")

    async def test_same_request_twice_is_checked_once(self):
        request = disposal(self.store_trip())
        self.assertEqual((await self.post(request)).status_code, 202)
        again = await self.post(request)
        self.assertEqual((again.status_code, again.json()["duplicate"]), (202, True))
        await self.work()
        self.assertEqual(len(self.gw.calls), 1)
        self.assertEqual(len(self.jev.requests), 8, "Jev asked about each image once")

    async def test_edit_is_checked_again(self):
        uploads = self.store_trip()
        first = disposal(uploads)
        edit = disposal({**uploads, "bill": []}, reason="edited", data_version="sha256:bbb")  # the bills taken off
        await self.post(first)
        await self.post(edit)
        await self.work()
        self.assertEqual(self.gw.stored[first["requestId"]]["outcome"], "passed")
        self.assertEqual(self.gw.stored[edit["requestId"]]["subject"]["dataVersion"], "sha256:bbb")
        self.assertEqual(self.gw.stored[edit["requestId"]]["outcome"], "flagged")
        self.assertIn("bill_missing", [f["code"] for f in self.gw.stored[edit["requestId"]]["flags"]])

    async def test_wrong_or_old_signature_is_refused(self):
        for kw in ({"secret": "wrong"}, {"now": time.time() - 600}):
            with self.assertLogs("verifier", "WARNING"):
                response = await self.post(disposal(), **kw)
            self.assertEqual(response.status_code, 401)
        self.assertEqual(await self.sql("SELECT * FROM verification_request"), [])

    async def test_a_large_request_is_refused_before_anything_else(self):
        body = b'{"padding": "' + b"x" * (2 * 1024 * 1024) + b'"}'
        response = await self.client.post("/v1/verifications", content=body, headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(await self.sql("SELECT count(*) FROM verification_request"), [(0,)])

    async def test_the_next_secret_is_accepted_while_switching(self):
        request = disposal()
        body, headers = signed(request, secret="next-webhook-secret-of-32-chars-0")
        with mock.patch.object(self.svc, "settings", replace(SETTINGS, webhook_secret_next="next-webhook-secret-of-32-chars-0")):
            response = await self.client.post("/v1/verifications", content=body, headers=headers)
        self.assertEqual(response.status_code, 202)

    async def test_signature_is_checked_on_the_raw_bytes(self):
        request = disposal()
        body, headers = signed(request)
        body = json.dumps(request, indent=2).encode()  # same JSON, different bytes
        with self.assertLogs("verifier", "WARNING"):
            response = await self.client.post("/v1/verifications", content=body, headers=headers)
        self.assertEqual(response.status_code, 401)

    async def test_unusable_request_is_refused(self):
        cases = {"X-GW-Delivery": lambda r, h: h.update({"X-GW-Delivery": str(uuid.uuid4())}),
                 "schemaVersion": lambda r, h: r.update(schemaVersion=2),
                 "callbackUrl": lambda r, h: r.update(callbackUrl="https://elsewhere.example/steal"),
                 "numeric id": lambda r, h: r["subject"].update(id="1234")}
        for name, spoil in cases.items():
            request = disposal()
            headers = {"X-GW-Delivery": request["requestId"]}
            spoil(request, headers)
            body, signed_headers = signed(request)
            with self.subTest(name), self.assertLogs("verifier", "WARNING"):
                response = await self.client.post("/v1/verifications", content=body, headers={**signed_headers, **headers})
                self.assertEqual(response.status_code, 400)

    async def test_wrong_api_key_on_gw_sales_stops_and_alerts(self):
        self.svc.settings = Settings(**{**SETTINGS.__dict__, "api_key": "wrong"})
        request = disposal()
        await self.post(request)
        with self.assertLogs("verifier", "ERROR") as logs:
            await self.work()
        self.assertIn("ALERT", logs.output[0])
        row = await database.result(self.svc.pool, request["requestId"])
        self.assertEqual(row["last_status"], 401)
        self.assertIsNotNone(row["gave_up_at"])
        await self.make_due()
        await self.work()
        self.assertEqual(len(self.gw.calls), 1, "not retried")

    # The outbox

    async def test_gw_sales_down_is_retried(self):
        self.gw.statuses = [503, None]  # an error, then a timeout, then stored
        request = disposal()
        await self.post(request)
        with self.assertLogs("verifier", "WARNING"):
            await self.work()
        row = await database.result(self.svc.pool, request["requestId"])
        self.assertEqual((row["attempts"], row["last_status"], row["sent_at"]), (1, 503, None))
        [(wait,)] = await self.sql("SELECT extract(epoch FROM scheduled_at - now()) FROM procrastinate_jobs "
                                   "WHERE task_name = 'send_result' AND status = 'todo'")
        self.assertAlmostEqual(float(wait), 30, delta=5)
        for _ in range(2):
            await self.make_due()
            with self.assertNoLogs("verifier", "ERROR"):
                await self.work()
        row = await database.result(self.svc.pool, request["requestId"])
        self.assertEqual((row["attempts"], row["last_status"]), (3, 200))
        self.assertIn(request["requestId"], self.gw.stored)

    async def test_gives_up_after_24_hours(self):
        self.gw.statuses = [503, 503]
        request = disposal()
        await self.post(request)
        with self.assertLogs("verifier", "WARNING"):
            await self.work()
        await self.sql("UPDATE procrastinate_jobs SET attempts = 40, scheduled_at = now() "
                       "WHERE task_name = 'send_result'")  # as if retried all day
        with self.assertLogs("verifier", "ERROR") as logs:
            await self.work()
        self.assertTrue(any("ALERT" in line and "gave up" in line for line in logs.output))
        row = await database.result(self.svc.pool, request["requestId"])
        self.assertIn("24 hours", row["gave_up_why"])
        self.assertEqual(await self.sql("SELECT status FROM procrastinate_jobs WHERE task_name = 'send_result'"),
                         [("failed",)])

    async def test_refused_result_is_not_resent(self):
        for status in (404, 413, 422):
            self.gw.statuses = [status]
            request = disposal()
            await self.post(request)
            with self.subTest(status), self.assertLogs("verifier", "WARNING"):
                await self.work()
                row = await database.result(self.svc.pool, request["requestId"])
                self.assertIn(str(status), row["gave_up_why"])

    async def test_checking_error_is_retried_then_sent_as_error(self):
        request = disposal()
        with mock.patch.object(disposal_check, "check", side_effect=disposal_check.JevUnavailable("Jev down")):
            await self.post(request)
            with self.assertLogs("verifier", "WARNING"):
                await self.work()
            self.assertEqual(self.gw.calls, [])
            await self.sql("UPDATE procrastinate_jobs SET attempts = 10, scheduled_at = now() WHERE task_name = 'verify'")
            with self.assertLogs("verifier", "ERROR"):
                await self.work()
        result = self.gw.stored[request["requestId"]]
        self.assertEqual(result["outcome"], "error")
        self.assertEqual(result["error"]["code"], "JevUnavailable")
        self.assertNotIn("Jev down", json.dumps(result))

    async def test_a_bug_is_not_retried(self):
        request = disposal()
        with mock.patch.object(disposal_check, "check", side_effect=KeyError("weigh_bill")):
            await self.post(request)
            with self.assertLogs("verifier", "ERROR") as logs:
                await self.work()
        self.assertEqual(self.gw.stored[request["requestId"]]["outcome"], "error")
        self.assertTrue(any("ALERT" in line for line in logs.output))

    async def last_try(self):
        """Run the verify job's last try now."""
        await self.sql("UPDATE procrastinate_jobs SET attempts = 10, scheduled_at = now() WHERE task_name = 'verify'")
        await self.work()

    def photo_requests(self) -> int:
        """Requests the fake Jev got for the loading and unloading photos (make_trip's first four colours)."""
        return sum(self.jev.colour_of(sent(r)) in COLOURS[:4] for r in self.jev.requests)

    async def test_jev_down_for_one_image_retries_then_sends_the_rest(self):
        request = disposal(self.store_trip())
        self.jev.fail = {COLOURS[5]: 500}  # the final slip
        await self.post(request)
        with self.assertLogs("verifier", "WARNING"):
            await self.work()
        self.assertEqual(self.gw.stored, {}, "retried, not sent yet")
        asked = self.photo_requests()
        await self.last_try()
        self.assertEqual(self.photo_requests(), asked, "the photos' answers were saved, not asked again")
        result = self.gw.stored[request["requestId"]]
        self.assertEqual(result["outcome"], "flagged")
        unread = [f for f in result["flags"] if f["code"] == "image_unavailable"]
        self.assertEqual(sum((f["evidence"] for f in unread), []), ["disposal/2026/10/15/weighbill2.jpg"])

    async def test_jev_down_for_everything_sends_error(self):
        request = disposal(self.store_trip())
        self.jev.fail = {c: 500 for c in COLOURS[:6]}
        await self.post(request)
        with self.assertLogs("verifier", "WARNING"):
            await self.work()
        with self.assertLogs("verifier", "ERROR"):
            await self.last_try()
        self.assertEqual(self.gw.stored[request["requestId"]]["outcome"], "error")

    async def test_jev_refusing_every_image_sends_error_at_once(self):
        request = disposal(self.store_trip())
        self.jev.fail = {c: 422 for c in COLOURS[:6]}
        await self.post(request)
        with self.assertLogs("verifier", "ERROR"):
            await self.work()
        self.assertEqual(self.gw.stored[request["requestId"]]["outcome"], "error")

    async def test_missing_in_s3_and_unanswered_are_both_flagged(self):
        request = disposal(self.store_trip())
        boto3.client("s3").delete_object(Bucket=BUCKET, Key="collection-logs/2026/10/15/collection1.jpg")
        self.jev.fail = {COLOURS[5]: 500}
        await self.post(request)
        with self.assertLogs("verifier", "WARNING"):
            await self.work()
            await self.last_try()
        result = self.gw.stored[request["requestId"]]
        self.assertEqual(result["outcome"], "flagged")
        unread = sorted(sum((f["evidence"] for f in result["flags"] if f["code"] == "image_unavailable"), []))
        self.assertEqual(unread, ["collection-logs/2026/10/15/collection1.jpg", "disposal/2026/10/15/weighbill2.jpg"])

    async def test_sweep_queues_lost_work(self):
        lost_check, lost_send = disposal(), disposal()
        with mock.patch.object(tasks, "queue", side_effect=RuntimeError("queue down")), self.assertLogs("verifier", "ERROR"):
            self.assertEqual((await self.post(lost_check)).status_code, 202, "saved, so still accepted")
        await database.save_request(self.svc.pool, lost_send)
        await database.save_result(self.svc.pool, lost_send["requestId"], tasks.make_result(lost_send, "passed"))
        await self.sweep()
        self.assertEqual(await self.sql("SELECT count(*) FROM procrastinate_jobs"), [(0,)], "too recent to be lost")
        await self.sql("UPDATE verification_request SET received_at = now() - interval '5 minutes'")
        await self.sql("UPDATE verification_result SET made_at = now() - interval '5 minutes'")
        with self.assertLogs("verifier", "WARNING"):
            await self.sweep()
            await self.sweep()  # queued once only
        self.assertEqual(sorted(await self.sql("SELECT task_name FROM procrastinate_jobs")), [("send_result",), ("verify",)])
        await self.work()
        self.assertEqual(set(self.gw.stored), {lost_check["requestId"], lost_send["requestId"]})

    async def test_each_row_keeps_its_job_and_a_removed_job_is_queued_again(self):
        request = disposal()
        self.gw.statuses = [503]
        await self.post(request)
        [(verify_job,)] = await self.sql("SELECT verify_job FROM verification_request")
        self.assertIsNotNone(verify_job)
        with self.assertLogs("verifier", "WARNING"):
            await self.work()
        [(send_job,)] = await self.sql("SELECT send_job FROM verification_result")
        self.assertIsNotNone(send_job)
        await self.sql("UPDATE verification_result SET made_at = now() - interval '5 minutes'")
        await self.sweep()  # its retry is waiting: left alone
        self.assertEqual(await self.sql("SELECT count(*) FROM procrastinate_jobs WHERE task_name = 'send_result'"), [(1,)])
        await self.sql("DELETE FROM procrastinate_jobs WHERE id = %s", send_job)  # as if cleaned away
        with self.assertLogs("verifier", "WARNING") as logs:
            await self.sweep()
        self.assertIn("lost", logs.output[0])
        [(again,)] = await self.sql("SELECT send_job FROM verification_result")
        self.assertNotEqual(again, send_job)
        await self.work()
        self.assertIn(request["requestId"], self.gw.stored)

    async def sweep(self):
        await tasks.sweep(timestamp=int(time.time()))

    async def make_old(self):
        """Make every request and result old enough for the sweep to look at."""
        await self.sql("UPDATE verification_request SET received_at = now() - interval '5 minutes'")
        await self.sql("UPDATE verification_result SET made_at = now() - interval '5 minutes'")

    def failing(self, target, times: int):
        """Patch `database.<target>` to raise for its first `times` calls, as a database blip would."""
        real, calls = getattr(database, target), []

        async def flaky(*args, **kw):
            calls.append(1)
            if len(calls) <= times:
                raise RuntimeError("database blip")
            return await real(*args, **kw)

        return mock.patch.object(database, target, flaky)

    async def test_a_verify_that_crashed_outside_its_try_is_queued_again(self):
        request = disposal(self.store_trip())
        with self.failing("save_result", 1):
            await self.post(request)
            with self.assertLogs("procrastinate", "ERROR"):  # the job crashes: Procrastinate logs it
                await self.work()
            self.assertEqual(self.gw.stored, {})
            await self.make_old()
            with self.assertLogs("verifier", "ERROR") as logs:
                await self.sweep()
            self.assertTrue(any("ALERT" in line for line in logs.output))
            await self.work()
        self.assertEqual(self.gw.stored[request["requestId"]]["outcome"], "passed")

    async def test_a_verify_that_keeps_crashing_ends_as_error(self):
        request = disposal(self.store_trip())
        with self.failing("save_result", 1 + tasks.MAX_REQUEUES), self.assertLogs("verifier", "WARNING"):
            await self.post(request)
            await self.work()
            await self.make_old()
            for _ in range(tasks.MAX_REQUEUES):
                await self.sweep()
                await self.work()
            await self.sweep()  # at the cap: an error result is saved and sent
            await self.work()
        self.assertEqual(self.gw.stored[request["requestId"]]["outcome"], "error")

    async def test_a_job_whose_worker_keeps_dying_is_given_up(self):
        # A file that kills the worker (out of memory, a native decoder crashing) leaves its job stalled, not failed:
        # it is retried, counted like any other requeue, and given up at the cap.
        request = disposal()
        await database.save_request(self.svc.pool, request)
        rid = request["requestId"]
        stalled = mock.Mock(id=1, task_name="verify", task_kwargs={"request_id": rid})
        manager = tasks.app.job_manager
        with mock.patch.object(manager, "get_stalled_jobs", mock.AsyncMock(return_value=[stalled])), \
                mock.patch.object(manager, "retry_job", mock.AsyncMock()) as retried, \
                mock.patch.object(manager, "finish_job_by_id_async", mock.AsyncMock()) as finished, \
                self.assertLogs("verifier", "WARNING"):
            for _ in range(tasks.MAX_REQUEUES):
                await self.sweep()
            self.assertEqual((retried.await_count, finished.await_count), (tasks.MAX_REQUEUES, 0))
            await self.sweep()
            finished.assert_awaited_once()
        self.assertEqual((await database.result(self.svc.pool, rid))["body"]["outcome"], "error")

    async def test_a_send_that_crashed_is_sent_again(self):
        request = disposal(self.store_trip())
        with self.failing("note_attempt", 1):
            await self.post(request)
            with self.assertLogs("procrastinate", "ERROR"):  # the job crashes: Procrastinate logs it
                await self.work()
            await self.make_old()
            with self.assertLogs("verifier", "ERROR"):
                await self.sweep()
            await self.work()
        self.assertEqual(len(self.gw.calls), 2, "sent again: gw_sales answers a repeated requestId with 200")
        self.assertIsNotNone((await database.result(self.svc.pool, request["requestId"]))["sent_at"])

    async def test_a_send_that_keeps_crashing_is_given_up(self):
        request = disposal(self.store_trip())
        with self.failing("note_attempt", 1 + tasks.MAX_REQUEUES), self.assertLogs("verifier", "WARNING"):
            await self.post(request)
            await self.work()
            await self.make_old()
            for _ in range(tasks.MAX_REQUEUES):
                await self.sweep()
                await self.work()
            await self.sweep()
        self.assertIsNotNone((await database.result(self.svc.pool, request["requestId"]))["gave_up_at"])

    # Comparing disposals

    async def test_a_second_disposal_with_the_same_photos_names_the_first(self):
        uploads = self.store_trip()
        await self.post(disposal(uploads, disposal_id=1000))
        await self.work()
        again = disposal(uploads, disposal_id=1001)
        await self.post(again)
        [(lock,)] = await self.sql("SELECT lock FROM procrastinate_jobs WHERE task_name = 'verify' AND status = 'todo'")
        self.assertEqual(lock, f"vehicle:{NUMBER}", "one vehicle's checks run one at a time")
        await self.work()
        flags = {f["code"]: f for f in self.gw.stored[again["requestId"]]["flags"]}
        self.assertEqual(flags["reused"]["related"], [{"type": "disposal_log", "id": 1000}])
        self.assertNotIn("overlap", flags, "the pair is flagged once, as reused")
        self.assertEqual(await self.sql("SELECT vehicle_number, quantity, trip_day FROM verification_request "
                                        "WHERE request_id = %s", again["requestId"]),
                         [(NUMBER, f"{QUANTITY} kg", date(2026, 10, 15))])

    async def test_an_edit_is_not_compared_with_its_own_earlier_request(self):
        uploads = self.store_trip()
        await self.post(disposal(uploads))
        await self.work()
        edit = disposal(uploads, reason="edited", data_version="sha256:bbb")
        await self.post(edit)
        await self.work()
        self.assertEqual(self.gw.stored[edit["requestId"]]["outcome"], "passed")

    async def test_a_copy_of_another_vehicles_older_disposal_is_flagged(self):
        uploads = self.store_trip()
        await self.post(disposal(uploads, disposal_id=1000, vehicle={"id": 13, "vehicleNumber": "KL07BC1234"},
                                 arrivalTime="2026-09-01T03:00:00.000Z"))
        await self.work()
        again = disposal(uploads, disposal_id=1001)
        await self.post(again)
        await self.work()
        flag = {f["code"]: f for f in self.gw.stored[again["requestId"]]["flags"]}["reused"]
        self.assertEqual(flag["related"], [{"type": "disposal_log", "id": 1000}])
        self.assertIn("(KL07BC1234, 01/09)", flag["message"])

    async def test_a_copy_of_a_disposal_whose_check_ended_in_an_error_is_flagged(self):
        uploads = self.store_trip()
        first = disposal(uploads, disposal_id=1000)
        await database.save_request(self.svc.pool, first)
        await database.save_prints(self.svc.pool, first["requestId"],
                                   [("loading", k, proof_check.fingerprints(v)["md5"], "00" * 8)
                                    for k, v in files(uploads).items() if "collection" in k])
        await database.save_result(self.svc.pool, first["requestId"], tasks.make_result(first, "error", error={"code": "X", "message": "x"}))
        again = disposal(uploads, disposal_id=1001)
        await self.post(again)
        await self.work()
        self.assertIn("reused", {f["code"] for f in self.gw.stored[again["requestId"]]["flags"]})

    async def test_two_vehicles_checked_at_once_the_later_finds_the_earlier(self):
        # Disposal 1000 (another vehicle) is still being checked: request and prints saved, no result yet.
        uploads = self.store_trip()
        first = disposal(uploads, disposal_id=1000, vehicle={"id": 13, "vehicleNumber": "KL07BC1234"})
        await database.save_request(self.svc.pool, first)
        await database.save_prints(self.svc.pool, first["requestId"],
                                   [("bill", k, proof_check.fingerprints(v)["md5"], "00" * 8) for k, v in files(uploads).items()])
        again = disposal(uploads, disposal_id=1001)
        await self.post(again)
        await self.work()
        self.assertIn("reused", {f["code"] for f in self.gw.stored[again["requestId"]]["flags"]})

    async def test_a_retried_request_never_matches_itself(self):
        uploads = self.store_trip()
        request = disposal(uploads)
        await self.post(request)
        await self.work()
        async with self.svc.pool.connection() as conn:  # as a retry would: the same prints saved again
            rows = await (await conn.execute("SELECT kind, name, md5, dhash FROM image_print WHERE request_id = %s",
                                              (request["requestId"],))).fetchall()
        self.assertTrue(rows)
        await database.save_prints(self.svc.pool, request["requestId"], [tuple(r) for r in rows])
        self.assertEqual(self.gw.stored[request["requestId"]]["outcome"], "passed")
        self.assertEqual(await database.copies(self.svc.pool, DISPOSAL, [r[2] for r in rows]), [])

    async def test_a_disposal_is_compared_as_last_edited(self):
        uploads = self.store_trip()
        await self.post(disposal(uploads, disposal_id=1000))
        await self.work()
        await self.post(disposal(uploads, disposal_id=1000, reason="edited", data_version="sha256:bbb",
                                 vehicle={"id": 13, "vehicleNumber": "kl 07 bc 1234"}))
        await self.work()
        again = disposal(uploads, disposal_id=1001)
        await self.post(again)
        await self.work()
        codes = {f["code"] for f in self.gw.stored[again["requestId"]]["flags"]}
        for code in ("overlap", "same_weighing", "same_moment"):
            self.assertNotIn(code, codes, "1000 is now another vehicle's")
        self.assertEqual(await database.others(self.svc.pool, 1001, NUMBER, date(2026, 10, 15)), [],
                         "1000's latest request is now KL07BC1234's")
        flag = {f["code"]: f for f in self.gw.stored[again["requestId"]]["flags"]}["reused"]
        self.assertIn("(KL07BC1234", flag["message"])

    async def test_a_disposal_whose_last_edit_is_not_checked_yet_is_left_out(self):
        uploads = self.store_trip()
        await self.post(disposal(uploads, disposal_id=1000))
        await self.work()
        await database.save_request(self.svc.pool, disposal(uploads, disposal_id=1000, reason="edited",
                                                            data_version="sha256:bbb"))
        self.assertEqual(await database.others(self.svc.pool, 1001, NUMBER, date(2026, 10, 15)), [],
                         "its earlier photos may have been changed")

    async def test_others_carry_the_quantity(self):
        await self.post(disposal(self.store_trip(), disposal_id=1000))
        await self.work()
        rows = await database.others(self.svc.pool, 1001, NUMBER, date(2026, 10, 15))
        self.assertEqual([(i, q) for i, _, q in rows], [(1000, f"{QUANTITY} kg")])

    async def test_only_disposals_within_a_day_are_compared(self):
        uploads = self.store_trip()
        await self.post(disposal(uploads, disposal_id=1000, arrivalTime="2026-10-13T03:00:00.000Z"))  # two days before
        await self.work()
        again = disposal(uploads, disposal_id=1001)
        await self.post(again)
        await self.work()
        codes = {f["code"] for f in self.gw.stored[again["requestId"]]["flags"]}
        for code in ("overlap", "same_moment", "same_weighing"):
            self.assertNotIn(code, codes, "the look-alike comparisons stay within a day")
        self.assertEqual(await database.others(self.svc.pool, 1001, NUMBER, date(2026, 10, 15)), [],
                         "1000 is two days earlier")
        self.assertIn("reused", codes, "an exact copy is found on any date")

    async def test_answers_are_saved_as_they_come(self):
        request = disposal()
        await database.save_request(self.svc.pool, request)
        rid = request["requestId"]
        answers = database.SavedAnswers(self.svc.pool, asyncio.get_running_loop(), rid, {})

        def in_the_worker_thread():
            answers["k1"] = {"a": 1}
            answers["k1"] = {"a": 1}  # an image asked again after a crash: kept once, no error
            return "k1" in answers, answers["k1"]

        self.assertEqual(await asyncio.to_thread(in_the_worker_thread), (True, {"a": 1}))
        self.assertEqual(await database.saved_answers(self.svc.pool, rid), {"k1": {"a": 1}})
        self.assertEqual(await database.saved_answers(self.svc.pool, str(uuid.uuid4())), {})

    async def test_the_sweep_looks_back_a_week(self):
        old = disposal()
        await database.save_request(self.svc.pool, old)
        await self.sql("UPDATE verification_request SET received_at = now() - interval '8 days'")
        waiting = await database.waiting(self.svc.pool, datetime.now(standards.IST))
        self.assertEqual(waiting["verify"], [], "older than a week: the daily report counts it instead")
        report = await database.day_report(self.svc.pool, datetime.now(standards.IST).date())
        self.assertEqual(report["requestsUnchecked"], 1)

    async def test_any_2xx_from_gw_sales_is_delivered(self):
        request = disposal(self.store_trip())
        self.gw.statuses = [204]
        await self.post(request)
        with self.assertNoLogs("verifier", "ERROR"):
            await self.work()
        self.assertIsNotNone((await database.result(self.svc.pool, request["requestId"]))["sent_at"])

    async def test_the_sweep_beats_and_the_web_alerts_when_the_worker_is_silent(self):
        await self.sweep()
        beat = await database.last_beat(self.svc.pool)
        self.assertLess((datetime.now(standards.IST) - beat).total_seconds(), 60)
        now, started = beat + timedelta(minutes=6), beat - timedelta(hours=1)
        with self.assertLogs("verifier", "ERROR") as logs:
            alerted = await api.watch_worker(self.svc.pool, started, None, now)
        self.assertIn("ALERT worker", logs.output[0])
        with self.assertNoLogs("verifier", "ERROR"):
            self.assertEqual(await api.watch_worker(self.svc.pool, started, alerted, now + timedelta(minutes=10)), alerted)
        with self.assertLogs("verifier", "ERROR"):
            await api.watch_worker(self.svc.pool, started, alerted, now + timedelta(minutes=31))
        with self.assertNoLogs("verifier", "ERROR"):
            self.assertIsNone(await api.watch_worker(self.svc.pool, started, alerted, beat + timedelta(minutes=1)))

    async def test_health_and_ready(self):
        response = await self.client.get("/health")
        self.assertEqual((response.status_code, response.json()), (200, {"ok": True}))
        response = await self.client.get("/ready")
        self.assertEqual((response.status_code, response.json()), (200, {"ok": True}))

    async def test_day_report(self):
        self.gw.statuses = [503]  # the second result waits to be delivered
        for request in (disposal(self.store_trip()), disposal(disposal_id=1001)):
            await self.post(request)
        with self.assertLogs("verifier", "WARNING"):
            await self.work()
        today = datetime.now(standards.IST).date()
        report = await database.day_report(self.svc.pool, today)
        self.assertEqual(report["received"], {"created": 2})
        self.assertEqual(sum(report["outcomes"].values()), 2)
        self.assertEqual(report["resultsWaiting"], 1)
        self.assertEqual(report["requestsUnchecked"], 0)
        await database.save_request(self.svc.pool, disposal(disposal_id=1002))  # never checked
        self.assertEqual((await database.day_report(self.svc.pool, today))["requestsUnchecked"], 1)
        self.assertGreater(report["jevCostUsd"], 0)
        self.assertIn("reused", report["flags"], "disposal 1001 lists the same S3 files as the first disposal")
        with self.assertLogs("verifier", "INFO") as logged:
            await tasks.daily_report(timestamp=int(time.time()) + 24 * 3600)  # just after midnight tomorrow: today's
        self.assertTrue(any("daily report" in line and '"resultsWaiting": 1' in line for line in logged.output))
        self.assertTrue(any(line.startswith("ERROR") and "ALERT daily report" in line for line in logged.output))


if __name__ == "__main__":
    unittest.main()
