"""Offline tests of the verifier service: gw_sales' end-to-end cases (signed request in, signed result out,
duplicates, edits, wrong secrets), the outbox's retries and recovery, and disposals checked with proof_check,
against a fake gw_sales, a fake S3 (moto) and the fake Jev of test_offline.

The service tests need the dev Postgres (README: verifier); they are skipped when it isn't running.

Run:
    .venv/bin/python -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import time
import unittest
import uuid
from datetime import date
from pathlib import Path
from unittest import mock

import boto3
import httpx
import psycopg
from moto import mock_aws

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import proof_check  # noqa: E402
from proof_check import Upload  # noqa: E402
from test_proof_check import (COLOURS, FINAL, FIRST, LOADING, NUMBER, QUANTITY, UNLOADING, FakeJev, at, coloured,  # noqa: E402
                          make_trip, sent)
from verifier import api, compare_disposals, database, disposal_check, migrate, signatures, standards, tasks  # noqa: E402
from verifier.settings import Settings  # noqa: E402

DB = os.environ.get("VERIFIER_TEST_DATABASE_URL", "postgresql://verifier@127.0.0.1:5433/verifier_test")
GW = "https://dev-sales.example"
BUCKET = "gw-photos"
SETTINGS = Settings(database_url=DB, webhook_secret="webhook-secret", callback_secret="callback-secret",
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
    for kind, items, name in (("loading", (loading, loading), "collection"), ("unloading", (unloading, unloading), "unloading"),
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
        self.assertEqual(standards.ist_day("2026-10-27 18:29:59"), date(2026, 10, 27), "no zone: taken as UTC")


class StandardTests(unittest.TestCase):
    def test_the_standard_of_the_trips_day(self):
        later = standards.Standard("2", date(2026, 12, 1), proof_check.Limits(clock_slack=10))
        with mock.patch.object(standards, "STANDARDS", [*standards.STANDARDS, later]):
            self.assertEqual(standards.for_day(date(2026, 11, 30)).version, "1")
            self.assertEqual(standards.for_day(date(2026, 12, 1)).version, "2")
            self.assertEqual(standards.for_day(date(2020, 1, 1)).version, "1")

    def test_models_are_pinned(self):
        described = standards.STANDARDS[0].describe()
        self.assertEqual(described["models"], {"questions": "jev-1.14.0", "box": "jev-1.14.0"})
        self.assertEqual(described["limits"]["min_gap_tare_first"], 60)


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
        self.assertEqual(checked.standard["version"], "1")
        self.assertEqual(checked.standard["models"]["questions"], "jev-1.14.0")
        self.assertTrue(jev.requests)
        self.assertTrue(all(b'"model":"jev-1.14.0"' in sent(r) for r in jev.requests), "pinned models only")
        self.assertEqual(checked.extractions["parts"], {"real": "pass", "docs": "pass"})
        self.assertIn("disposal/2026/10/15/weighbill2.jpg", checked.extractions["images"])
        self.assertGreater(checked.cost_usd, 0)
        json.dumps(checked.analysis, default=str)

    def test_flags_carry_code_evidence_and_severity(self):
        wrong = {**FINAL, "bill_number": ("different", 0.92)}
        uploads, jev = make_trip(bill=(FINAL, wrong))
        checked = self.check(uploads, jev)
        self.assertEqual(checked.outcome, "flagged")
        first = checked.flags[0]
        self.assertEqual((first["code"], first["severity"], first["kind"]), ("bill_number", "high", "problem found"))
        self.assertIn("disposal/2026/10/15/weighbill2.jpg", first["evidence"])
        self.assertTrue(first["message"])

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

    def test_nothing_in_storage_needs_no_jev(self):
        jev = FakeJev()
        with self.assertLogs("verifier", "WARNING"):
            checked = self.check(NOT_STORED, jev, stored={})
        self.assertEqual(jev.requests, [])
        self.assertEqual(checked.outcome, "flagged")
        self.assertTrue(all(f["kind"] == "please check" for f in checked.flags))


class CompareTests(unittest.TestCase):
    """A disposal compared with its vehicle's other disposals."""

    def checked(self, uploads, jev, others=(), disposal_id=DISPOSAL, **claims):
        with jev.client() as client:
            return disposal_check.check(disposal(uploads, disposal_id=disposal_id, **claims), files(uploads).get, client,
                                 others=list(others))

    def first(self):
        """Disposal 1000, already checked: make_trip's images, weighed 02:00-03:30."""
        uploads, jev = make_trip()
        return compare_disposals.Other(1000, f"{QUANTITY} kg", self.checked(uploads, jev, disposal_id=1000).analysis)

    def flag(self, checked, code):
        return next((f for f in checked.flags if f["code"] == code), None)

    def test_same_images_again_are_a_problem_naming_the_other_disposal(self):
        uploads, jev = other_trip(colours=0, bill=LATER)  # make_trip's files, renamed
        checked = self.checked(uploads, jev, [self.first()])
        flag = self.flag(checked, "reused")
        self.assertEqual((flag["kind"], flag["severity"]), ("problem found", "high"))
        self.assertEqual(flag["related"], [{"type": "disposal_log", "id": 1000}])
        self.assertIn("is the same file as collection-logs/2026/10/15/collection1.jpg in disposal 1000", flag["message"])
        self.assertIn("collection-logs/2026/10/15/b-collection1.jpg", flag["evidence"])
        self.assertIsNone(self.flag(checked, "same_moment"), "copies are reused's, not also the same moment")

    def test_look_alike_asks_a_person(self):
        uploads, jev = other_trip(colours=0, quality=60, bill=LATER)
        flag = self.flag(self.checked(uploads, jev, [self.first()]), "reused")
        self.assertEqual((flag["kind"], flag["severity"]), ("please check", "recheck"))
        self.assertIn("looks the same as", flag["message"])

    def test_images_of_another_quantity_are_not_compared(self):
        uploads, jev = other_trip(colours=0, bill=LATER)
        checked = self.checked(uploads, jev, [self.first()], quantity=999)
        self.assertIsNone(self.flag(checked, "reused"))

    def test_photos_at_the_same_moment_ask_a_person(self):
        uploads, jev = other_trip(bill=LATER)  # other images, stamped as make_trip's
        checked = self.checked(uploads, jev, [self.first()])
        flag = self.flag(checked, "same_moment")
        self.assertEqual((flag["kind"], flag["severity"]), ("please check", "recheck"))
        self.assertEqual(flag["related"], [{"type": "disposal_log", "id": 1000}])
        self.assertIn("b-collection1.jpg was stamped 02:40 on 15/10", flag["message"])
        self.assertIsNone(self.flag(checked, "overlap"), "weighed later: no overlap")
        self.assertIsNone(self.flag(checked, "reused"))

    def test_more_than_five_minutes_apart_is_not_the_same_moment(self):
        later = {**LOADING, "stamp_minute": ("46", 0.95)}  # 02:46 against 02:40
        uploads, jev = other_trip(loading=later, unloading={**UNLOADING, "stamp_hour": ("05", 0.95)}, bill=LATER)
        self.assertIsNone(self.flag(self.checked(uploads, jev, [self.first()]), "same_moment"))

    def test_overlapping_weighings_are_a_problem_whatever_the_quantity(self):
        uploads, jev = other_trip()  # weighed 02:00-03:30, as disposal 1000
        checked = self.checked(uploads, jev, [self.first()], quantity=999)
        flag = self.flag(checked, "overlap")
        self.assertEqual((flag["kind"], flag["severity"]), ("problem found", "high"))
        self.assertIn("KL05AF9341 is weighed 02:00-03:30 on 15/10 for this disposal, overlapping disposal 1000", flag["message"])
        self.assertEqual(flag["related"], [{"type": "disposal_log", "id": 1000}])

    def test_several_trips_a_day_are_not_an_overlap(self):
        uploads, jev = other_trip(bill=LATER)  # weighed 05:00-06:30, after disposal 1000's 02:00-03:30
        checked = self.checked(uploads, jev, [self.first()], quantity=999)
        self.assertEqual(next(c for c in checked.analysis["verdict"]["checks"] if c["key"] == "overlap")["status"], "pass")

    def test_a_stored_tare_is_no_window(self):
        # Tare from the day before (a stored one): the trip's weighings would span a day and overlap disposal 1000.
        stored = (FIRST, {**FINAL, **at("tare", "05:00", "14/10"), **at("gross", "06:30")})
        uploads, jev = other_trip(bill=stored)
        checked = self.checked(uploads, jev, [self.first()], quantity=999)
        self.assertEqual(self.flag(checked, "weigh_day")["kind"], "problem found")
        self.assertIsNone(checked.analysis["verdict"]["window"])
        self.assertIsNone(self.flag(checked, "overlap"))

    def test_alone_every_comparison_passes(self):
        uploads, jev = make_trip()
        checks = {c["key"]: c["status"] for c in self.checked(uploads, jev).analysis["verdict"]["checks"]}
        self.assertEqual({k: checks[k] for k in ("same_file", "reused", "same_moment", "overlap")},
                         dict.fromkeys(("same_file", "reused", "same_moment", "overlap"), "pass"))

    def test_one_file_as_collection_and_unloading_photo_is_a_problem(self):
        uploads, jev = make_trip(same=(("loading", 0, "unloading", 0),))
        flag = self.flag(self.checked(uploads, jev), "same_file")
        self.assertEqual((flag["kind"], flag["severity"]), ("problem found", "high"))
        self.assertIn("collection-logs/2026/10/15/collection1.jpg and disposal/2026/10/15/unloading1.jpg", flag["message"])

    def test_without_a_vehicle_number_only_the_disposal_itself_is_checked(self):
        uploads, jev = other_trip(colours=0, bill=LATER)
        checked = self.checked(uploads, jev, [self.first()], vehicle={"vehicleNumber": ""})
        keys = {c["key"] for c in checked.analysis["verdict"]["checks"]}
        self.assertIn("same_file", keys)
        self.assertFalse(keys & {"reused", "same_moment", "overlap"})


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
        recorded = await self.recorded()
        self.assertTrue({"procrastinate/" + p.name for p in migrate.procrastinate_migrations()} <= recorded)
        self.assertEqual(await self.migrate(), [])

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
        with mock.patch.object(disposal_check, "check", side_effect=RuntimeError("Jev down")):
            await self.post(request)
            with self.assertLogs("verifier", "WARNING"):
                await self.work()
            self.assertEqual(self.gw.calls, [])
            await self.sql("UPDATE procrastinate_jobs SET attempts = 10, scheduled_at = now() WHERE task_name = 'verify'")
            with self.assertLogs("verifier", "ERROR"):
                await self.work()
        result = self.gw.stored[request["requestId"]]
        self.assertEqual(result["outcome"], "error")
        self.assertEqual(result["error"]["code"], "RuntimeError")
        self.assertNotIn("Jev down", json.dumps(result))

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
        self.assertEqual(flags["overlap"]["related"], [{"type": "disposal_log", "id": 1000}])
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

    async def test_only_disposals_within_a_day_are_compared(self):
        uploads = self.store_trip()
        await self.post(disposal(uploads, disposal_id=1000, arrivalTime="2026-10-13T03:00:00.000Z"))  # two days before
        await self.work()
        again = disposal(uploads, disposal_id=1001)
        await self.post(again)
        await self.work()
        self.assertEqual(self.gw.stored[again["requestId"]]["outcome"], "passed")

    async def test_health(self):
        response = await self.client.get("/health")
        self.assertEqual((response.status_code, response.json()), (200, {"ok": True}))


if __name__ == "__main__":
    unittest.main()
