"""Offline tests of the verifier service: gw_sales' end-to-end cases (signed request in, signed result out,
duplicates, edits, wrong secrets) and the outbox's retries and recovery, against a fake gw_sales.

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
from pathlib import Path
from unittest import mock

import httpx
import psycopg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from verifier import checker, jobs, schema, signing, store, web  # noqa: E402
from verifier.config import Settings  # noqa: E402

DB = os.environ.get("VERIFIER_TEST_DATABASE_URL", "postgresql://verifier@127.0.0.1:5433/verifier_test")
GW = "https://dev-sales.example"
SETTINGS = Settings(database_url=DB, webhook_secret="webhook-secret", callback_secret="callback-secret",
                    api_key="gws_test_key", gw_sales_url=GW)
DISPOSAL = 1234


def disposal(request_id: str | None = None, reason: str = "created", data_version: str = "sha256:aaa",
             categories=("weighbill", "unloading", "collection")) -> dict:
    """A request as gw_sales sends it, cut down to what the service reads."""
    owners = {"collection": {"type": "collection_log", "id": 45001}}
    return {
        "requestId": request_id or str(uuid.uuid4()), "reason": reason, "schemaVersion": 1,
        "subject": {"type": "disposal_log", "id": DISPOSAL, "createdVia": "DISPOSAL_ENTRY",
                    "createdAt": "2026-10-27T06:10:04.000Z", "dataVersion": data_version, "status": "approval_pending"},
        "callbackUrl": f"{GW}/api/procurement/disposal-logs/{DISPOSAL}/verification-results",
        "claims": {"quantity": 1520.5, "unit": "kg", "vehicle": {"id": 12, "vehicleNumber": "KL11AB1234"}},
        "evidence": [{"owner": owners.get(c, {"type": "disposal_log", "id": DISPOSAL}), "category": c,
                      "s3Key": f"disposal/2026/10/27/{c}-{i}.jpg", "mimeType": "image/jpeg",
                      "uploadedAt": "2026-10-27T06:09:41.000Z"} for i, c in enumerate(categories)],
        "reference": {"collectionLogs": []},
    }


def signed(request: dict, secret: str = SETTINGS.webhook_secret, now: float | None = None) -> tuple[bytes, dict]:
    body = json.dumps(request).encode()
    return body, {"Content-Type": "application/json", "X-GW-Event": web.EVENT,
                  "X-GW-Delivery": request["requestId"], **signing.sign(body, secret, now)}


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
        if request.headers.get("x-api-key") != SETTINGS.api_key or not signing.verify(
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
        self.assertEqual(signing.signature(body, ts, "s"), expected)

    def test_verify(self):
        body, now = b'{"requestId":"x"}', 1_761_545_400
        headers = signing.sign(body, "s", now)
        ts, sig = headers["X-GW-Timestamp"], headers["X-GW-Signature"]
        self.assertTrue(signing.verify(body, ts, sig, "s", now=now + 299))
        self.assertFalse(signing.verify(body, ts, sig, "s", now=now + 301), "too old")
        self.assertFalse(signing.verify(body, ts, sig, "s", now=now - 301), "from the future")
        self.assertFalse(signing.verify(body + b" ", ts, sig, "s", now=now), "body changed")
        self.assertFalse(signing.verify(body, ts, sig, "other", now=now), "other secret")
        self.assertFalse(signing.verify(body, "soon", sig, "s", now=now))
        self.assertFalse(signing.verify(body, None, None, "s", now=now))


class CheckerTests(unittest.TestCase):
    def test_passes_with_every_kind_of_image(self):
        self.assertEqual(checker.check(disposal()), ("passed", []))

    def test_flags_each_missing_kind(self):
        with self.assertLogs("verifier", "INFO") as logs:
            outcome, flags = checker.check(disposal(categories=("unloading", "gps_photo")))
        self.assertEqual(outcome, "flagged")
        self.assertEqual([f["code"] for f in flags], ["bill_missing", "loading_missing"])
        self.assertEqual({(f["severity"], f["kind"]) for f in flags}, {("recheck", "problem found")})
        self.assertTrue(all(f["message"] for f in flags))
        self.assertIn("gps_photo", logs.output[0])

    def test_severity_by_part_and_status(self):
        self.assertEqual(checker.flag("bill_number", "problem", "x")["severity"], "high")
        self.assertEqual(checker.flag("bill_number", "review", "x")["severity"], "recheck")
        self.assertEqual(checker.flag("material", "problem", "x")["severity"], "recheck")
        self.assertEqual(checker.flag("material", "review", "x")["severity"], "info")
        self.assertEqual(checker.flag("bill_number", "review", "x", target={"type": "collection_log", "id": 1})["target"],
                         {"type": "collection_log", "id": 1})


class BackoffTests(unittest.TestCase):
    def test_send_retries_for_about_24_hours(self):
        job = mock.Mock()
        waits, attempts = [], 0
        while True:
            job.attempts = attempts
            decision = jobs.SEND_RETRY.get_retry_decision(exception=jobs.Unavailable(), job=job)
            if decision is None:
                break
            waits.append(jobs.SEND_RETRY.wait(attempts))
            attempts += 1
        self.assertEqual(waits[:3], [30, 60, 120])
        self.assertEqual(max(waits), 3600)
        self.assertTrue(23 * 3600 < sum(waits) <= 24 * 3600, sum(waits))

    def test_only_unavailable_is_retried_when_sending(self):
        job = mock.Mock(attempts=0)
        self.assertIsNone(jobs.SEND_RETRY.get_retry_decision(exception=ValueError(), job=job))


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
        async with jobs.running(self.settings) as svc:
            return await schema.migrate(svc.pool)

    async def recorded(self) -> set[str]:
        async with await psycopg.AsyncConnection.connect(self.settings.database_url) as conn:
            cur = await conn.execute("SELECT name FROM verifier_migration")
            return {row[0] for row in await cur.fetchall()}

    async def test_new_database_then_nothing_twice(self):
        applied = await self.migrate()
        self.assertTrue(applied[0].startswith("procrastinate 3."), applied)
        self.assertIn("verifier/001_requests_and_results.sql", applied)
        recorded = await self.recorded()
        self.assertTrue({"procrastinate/" + p.name for p in schema.procrastinate_migrations()} <= recorded)
        self.assertEqual(await self.migrate(), [])

    async def test_new_migration_is_applied_once(self):
        await self.migrate()
        with tempfile.TemporaryDirectory() as folder:
            extra = Path(folder) / "002_extra.sql"
            extra.write_text("ALTER TABLE verification_request ADD COLUMN extra text;")
            with mock.patch.object(schema, "our_migrations", lambda: [*sorted(schema.OURS.glob("0*.sql")), extra]):
                self.assertEqual(await self.migrate(), ["verifier/002_extra.sql"])
                self.assertEqual(await self.migrate(), [])
        self.assertIn("verifier/002_extra.sql", await self.recorded())

    async def test_failed_migration_is_not_recorded(self):
        await self.migrate()
        with tempfile.TemporaryDirectory() as folder:
            bad = Path(folder) / "002_bad.sql"
            bad.write_text("CREATE TABLE half_done (id int); SELECT no_such_function();")
            with mock.patch.object(schema, "our_migrations", lambda: [*sorted(schema.OURS.glob("0*.sql")), bad]):
                with self.assertRaises(psycopg.errors.UndefinedFunction):
                    await self.migrate()
        self.assertNotIn("verifier/002_bad.sql", await self.recorded())
        async with await psycopg.AsyncConnection.connect(self.settings.database_url) as conn:
            cur = await conn.execute("SELECT to_regclass('half_done')")
            self.assertIsNone((await cur.fetchone())[0], "rolled back whole")

    async def test_queue_tables_not_made_by_migrate_are_refused(self):
        async with await psycopg.AsyncConnection.connect(self.settings.database_url, autocommit=True) as conn:
            await conn.execute(jobs.app.schema_manager.get_schema())
        with self.assertRaises(SystemExit):
            await self.migrate()


@unittest.skipUnless(database_ready(), f"no Postgres at {DB}")
class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.gw = FakeGwSales()
        self.running = jobs.running(SETTINGS, httpx.MockTransport(lambda r: self.gw(r)))
        self.svc = await self.running.__aenter__()
        await schema.migrate(self.svc.pool)
        async with self.svc.pool.connection() as conn:
            await conn.execute("TRUNCATE verification_result, verification_request, procrastinate_jobs, "
                               "procrastinate_periodic_defers, procrastinate_workers CASCADE")
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=web.api), base_url="http://verifier")

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.running.__aexit__(None, None, None)

    async def post(self, request: dict, **kw) -> httpx.Response:
        body, headers = signed(request, **kw)
        return await self.client.post("/v1/verifications", content=body, headers=headers)

    async def work(self):
        """Run every job that is due, then stop."""
        await jobs.app.run_worker_async(wait=False, install_signal_handlers=False, listen_notify=False)

    async def sql(self, query: str, *args) -> list[tuple]:
        async with self.svc.pool.connection() as conn:
            cur = await conn.execute(query, args)
            return await cur.fetchall() if cur.description else []

    async def make_due(self):
        """Bring retries forward to now."""
        await self.sql("UPDATE procrastinate_jobs SET scheduled_at = now() WHERE status = 'todo'")

    # gw_sales' end-to-end cases

    async def test_new_disposal_gets_a_signed_result(self):
        request = disposal(categories=("unloading", "collection"))
        response = await self.post(request)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {"accepted": True, "duplicate": False})
        await self.work()
        self.assertEqual(len(self.gw.calls), 1)
        call = self.gw.calls[0]
        self.assertEqual(str(call.url), request["callbackUrl"])
        result = self.gw.stored[request["requestId"]]
        self.assertEqual(result["subject"], {"type": "disposal_log", "id": DISPOSAL, "dataVersion": "sha256:aaa"})
        self.assertEqual(result["outcome"], "flagged")
        self.assertEqual([f["code"] for f in result["flags"]], ["bill_missing"])
        self.assertEqual(result["verifier"]["name"], jobs.NAME)
        row = await store.result(self.svc.pool, request["requestId"])
        self.assertIsNotNone(row["sent_at"])
        self.assertEqual((row["attempts"], row["last_status"]), (1, 200))

    async def test_same_request_twice_is_checked_once(self):
        request = disposal()
        self.assertEqual((await self.post(request)).status_code, 202)
        again = await self.post(request)
        self.assertEqual((again.status_code, again.json()["duplicate"]), (202, True))
        await self.work()
        self.assertEqual(len(self.gw.calls), 1)
        self.assertEqual(self.gw.stored[request["requestId"]]["outcome"], "passed")

    async def test_edit_is_checked_again(self):
        first, edit = disposal(), disposal(reason="edited", data_version="sha256:bbb", categories=("unloading",))
        await self.post(first)
        await self.post(edit)
        await self.work()
        self.assertEqual(self.gw.stored[first["requestId"]]["outcome"], "passed")
        self.assertEqual(self.gw.stored[edit["requestId"]]["subject"]["dataVersion"], "sha256:bbb")
        self.assertEqual(self.gw.stored[edit["requestId"]]["outcome"], "flagged")

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
        row = await store.result(self.svc.pool, request["requestId"])
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
        row = await store.result(self.svc.pool, request["requestId"])
        self.assertEqual((row["attempts"], row["last_status"], row["sent_at"]), (1, 503, None))
        [(wait,)] = await self.sql("SELECT extract(epoch FROM scheduled_at - now()) FROM procrastinate_jobs "
                                   "WHERE task_name = 'send_result' AND status = 'todo'")
        self.assertAlmostEqual(float(wait), 30, delta=5)
        for _ in range(2):
            await self.make_due()
            with self.assertNoLogs("verifier", "ERROR"):
                await self.work()
        row = await store.result(self.svc.pool, request["requestId"])
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
        row = await store.result(self.svc.pool, request["requestId"])
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
                row = await store.result(self.svc.pool, request["requestId"])
                self.assertIn(str(status), row["gave_up_why"])

    async def test_checking_error_is_retried_then_sent_as_error(self):
        request = disposal()
        with mock.patch.object(checker, "check", side_effect=RuntimeError("Jev down")):
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
        with mock.patch.object(jobs, "queue", side_effect=RuntimeError("queue down")), self.assertLogs("verifier", "ERROR"):
            self.assertEqual((await self.post(lost_check)).status_code, 202, "saved, so still accepted")
        await store.save_request(self.svc.pool, lost_send)
        await store.save_result(self.svc.pool, lost_send["requestId"], jobs.make_result(lost_send, "passed"))
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
        await jobs.sweep(timestamp=int(time.time()))

    async def test_health(self):
        response = await self.client.get("/health")
        self.assertEqual((response.status_code, response.json()), (200, {"ok": True}))


if __name__ == "__main__":
    unittest.main()
