"""Offline tests of tools/replay_day: an export's images saved from fake signed links, a day checked into a replay
database with the fake Jev (in order of entry, compared with earlier disposals only, nothing sent), rechecks from
saved answers, the risky-pass list and the marks.

The database tests need the dev Postgres (README: verifier); they are skipped when it isn't running.

Run:
    .venv/bin/python -m unittest discover -s tests -v
"""

import asyncio
import json
import sys
import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import psycopg

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from test_proof_check import make_trip  # noqa: E402
from test_verifier import DB, LATER, database_ready, disposal, empty_database, files as stored, other_trip  # noqa: E402
from tools.replay_day import checking, files, report  # noqa: E402

DAY = date(2026, 10, 15)  # the test disposals' IST trip date
BEFORE = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)  # the links still work
EXPIRES = "2026-11-04T10:08:39.521Z"  # 15:38 IST


def export_of(*requests: dict, more: bool = False) -> dict:
    """gw_sales' export of `requests`: each without its callbackUrl, with a signed link beside every s3Key."""
    bodies = [{**{k: v for k, v in r.items() if k != "callbackUrl"},
               "evidence": [{**e, "signedUrl": f"https://gw.s3.ap-south-1.amazonaws.com/{e['s3Key']}?X-Amz-Signature=abc"}
                            for e in r["evidence"]]} for r in requests]
    return {"exportedAt": "2026-11-04T08:28:39.521Z", "schemaVersion": 1, "filters": {"startDate": "2026-10-15"},
            "signedUrlExpiresAt": EXPIRES, "count": len(bodies), "pageInfo": {"nextCursor": None, "hasNextPage": more},
            "disposals": bodies}


class FakeS3:
    """Signed links answered from `objects` by s3Key; keys in `refused` answer 403."""

    def __init__(self, objects: dict[str, bytes], refused=()):
        self.objects, self.refused, self.asked = objects, set(refused), []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        key = request.url.path.lstrip("/")
        self.asked.append(key)
        if key in self.refused or key not in self.objects:
            return httpx.Response(403, text="AccessDenied")
        return httpx.Response(200, content=self.objects[key])

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.trips = Path(tempfile.mkdtemp())
        self.uploads, _ = make_trip()
        self.request = disposal(self.uploads)
        self.s3 = FakeS3(stored(self.uploads))

    def download(self, export, now=BEFORE):
        with self.s3.client() as http:
            return files.download(export, self.trips, http, now, say=lambda line: None)

    def test_images_and_disposals_are_saved_without_their_links(self):
        done = self.download(export_of(self.request))
        self.assertEqual((done.days, done.images, done.failed), ({DAY: 1}, 6, []))
        folder = files.day_folder(DAY, self.trips)
        for item in self.request["evidence"]:
            self.assertTrue(files.image_path(folder, 1234, item["s3Key"]).is_file())
        saved = (folder / files.DISPOSALS).read_text()
        self.assertNotIn("signedUrl", saved)
        self.assertNotIn("X-Amz", saved)
        self.assertEqual(json.loads(saved)["1234"]["requestId"], self.request["requestId"])

    def test_expired_links_are_not_tried(self):
        with self.assertRaises(files.Problem) as raised:
            self.download(export_of(self.request), now=datetime(2026, 11, 4, 10, 9, tzinfo=UTC))
        self.assertIn("expired at 15:38 IST", str(raised.exception))
        self.assertIn("download", str(raised.exception))
        self.assertEqual(self.s3.asked, [])

    def test_a_failed_image_is_listed_and_only_it_fetched_from_a_new_export(self):
        key = self.request["evidence"][0]["s3Key"]
        self.s3.refused.add(key)
        done = self.download(export_of(self.request))
        self.assertEqual(len(done.failed), 1)
        self.assertIn("403", done.failed[0][2])
        self.s3.refused.clear()
        self.s3.asked.clear()
        self.download(export_of(self.request))
        self.assertEqual(self.s3.asked, [key])

    def test_only_amazon_s3_https_links_are_followed(self):
        export = export_of(self.request)
        export["disposals"][0]["evidence"][0]["signedUrl"] = "http://elsewhere.example/x.jpg"
        done = self.download(export)
        self.assertIn("isn't an Amazon S3 https link", done.failed[0][2])
        self.assertNotIn("x.jpg", " ".join(self.s3.asked))

    def test_a_file_that_isnt_an_image_is_not_saved(self):
        key = self.request["evidence"][0]["s3Key"]
        self.s3.objects[key] = b"<Error>not a photo</Error>"
        done = self.download(export_of(self.request))
        self.assertIn("isn't an image", done.failed[0][2])
        self.assertFalse(files.image_path(files.day_folder(DAY, self.trips), 1234, key).exists())

    def test_a_next_page_is_reported(self):
        self.assertTrue(self.download(export_of(self.request, more=True)).more_pages)
        cut_short = {**export_of(self.request), "truncated": True}
        cut_short.pop("pageInfo")
        self.assertTrue(self.download(cut_short).more_pages)
        self.assertFalse(self.download({**export_of(self.request), "truncated": False}).more_pages)

    def test_a_day_is_loaded_in_order_of_entry(self):
        later = disposal(self.uploads, disposal_id=1000)
        later["subject"]["createdAt"] = "2026-10-15T09:00:00.000Z"
        self.download(export_of(later, self.request))
        self.assertEqual([b["subject"]["id"] for b in files.load_day(DAY, self.trips)], [1234, 1000])

    def test_an_unsaved_day_says_to_download_first(self):
        with self.assertRaises(files.Problem) as raised:
            files.load_day(DAY, self.trips)
        self.assertIn("download", str(raised.exception))

    def test_not_an_export(self):
        path = self.trips / "x.json"
        path.write_text(json.dumps({"hello": 1}))
        with self.assertRaises(files.Problem):
            files.read_export(path)


class RiskyPassTests(unittest.TestCase):
    def test_a_pass_just_above_its_threshold_is_a_close_call(self):
        check = {"key": "loading_plate", "status": "pass", "certainty": 0.84, "measure": "confidence"}
        self.assertEqual(report.close_call(check, None, 0.1), "0.84 sure, needs 0.80")
        self.assertIsNone(report.close_call({**check, "certainty": 0.95}, None, 0.1))
        self.assertIsNone(report.close_call({**check, "status": "review"}, None, 0.1))
        self.assertIsNone(report.close_call({**check, "certainty": None}, None, 0.1))

    def test_a_yes_no_pass_is_measured_from_the_side_it_passed_on(self):
        yes = {"key": "gps_stamp", "status": "pass", "certainty": 0.85, "measure": "yes"}
        self.assertEqual(report.close_call(yes, "photo", 0.1), "yes, 0.85 sure, needs 0.80")
        self.assertIsNone(report.close_call({**yes, "certainty": 0.99}, "photo", 0.1))
        screen = {"key": "screen_photo", "status": "pass", "measure": "yes"}  # passes on no: flagged only from 0.8
        self.assertEqual(report.close_call({**screen, "certainty": 0.75}, "photo", 0.1), "no, 0.25 sure, needs 0.20")
        self.assertIsNone(report.close_call({**screen, "certainty": 0.55}, "photo", 0.1))

    def card(self, i, outcome="passed", risky=()):
        return {"id": i, "outcome": outcome, "risky": [{"sure": s} for s in risky]}

    def test_review_order_and_a_sample_that_stays_the_same_for_the_day(self):
        cards = [self.card(1), self.card(2, risky=[0.85]), self.card(3, "flagged"), self.card(4, risky=[0.81])] + \
                [self.card(i) for i in range(10, 20)]
        report.order(cards, DAY, 3)
        self.assertEqual([c["id"] for c in cards[:3]], [3, 4, 2])
        picked = [c["id"] for c in cards if c["group"] == "sample"]
        self.assertEqual(len(picked), 3)
        again = [self.card(1)] + [self.card(i) for i in range(10, 20)]
        report.order(again, DAY, 3)
        self.assertEqual([c["id"] for c in again if c["group"] == "sample"], picked)

    def test_marks_a_recheck_changed(self):
        cards = [{"id": 1, "outcome": "passed", "flags": []},
                 {"id": 2, "outcome": "flagged", "flags": [{"code": "net_weight", "about": "", "checks": ["net_weight"]}]}]
        marks = {(1, "loading_plate", ""): {"mark": "wrong"}, (1, "bill_number", ""): {"mark": "right"},
                 (2, "net_weight", "pass:bill"): {"mark": "missed"}}
        found = {(c["disposal"], c["check"]): (c["fixed"], c["what"]) for c in report.changes(cards, marks)}
        self.assertEqual(found, {(1, "loading_plate"): (True, "marked wrong, no longer flagged"),
                                 (1, "bill_number"): (False, "marked right, no longer flagged"),
                                 (2, "net_weight"): (True, "marked missed, now flagged")})


@unittest.skipUnless(database_ready(), f"no Postgres at {DB}")
class ReplayTests(unittest.IsolatedAsyncioTestCase):
    """A day checked into an empty replay database."""

    async def asyncSetUp(self):
        self.url = empty_database("verifier_replay_test")
        self.pool = await checking.open_database(self.url)
        self.trips = Path(tempfile.mkdtemp())
        first, jev = make_trip()
        copy, self.jev = other_trip(colours=0, bill=LATER)  # make_trip's files again, renamed, weighed later
        self.first = disposal(first, disposal_id=2001)
        self.copy = disposal(copy, disposal_id=2002)
        self.copy["subject"]["createdAt"] = "2026-10-15T09:00:00.000Z"  # entered after 2001
        with FakeS3({**stored(first), **stored(copy)}).client() as http:
            await asyncio.to_thread(files.download, export_of(self.copy, self.first), self.trips, http, BEFORE,
                                    say=lambda line: None)
        self.bodies = files.load_day(DAY, self.trips)

    async def asyncTearDown(self):
        await self.pool.close()

    async def check(self, saved_only=False, bodies=None):
        client = checking.saved_only_client() if saved_only else self.jev.client()
        with client:
            return await checking.check_day(self.pool, DAY, bodies or self.bodies, client, None, saved_only,
                                            say=lambda line: None, trips=self.trips)

    async def flags(self) -> dict[int, list[dict]]:
        saved = await checking.results(self.pool, self.bodies)
        return {i: r["result"]["flags"] for i, r in saved.items()}

    async def test_disposals_are_compared_only_with_ones_entered_before_them(self):
        outcomes = await self.check()
        self.assertEqual([o.disposal_id for o in outcomes], [2001, 2002])
        flags = await self.flags()
        reused = [f for f in flags[2002] if f["code"] == "reused"]
        self.assertEqual(reused[0]["related"], [{"type": "disposal_log", "id": 2001}])
        self.assertFalse([f for f in flags[2001] if f.get("related")], "2001 was entered first: it never sees 2002")
        self.assertGreater(sum(o.cost for o in outcomes), 0)

    async def test_nothing_is_sent(self):
        await self.check()
        async with self.pool.connection() as conn:
            cur = await conn.execute("SELECT q.callback_url, r.send_job, r.sent_at, r.attempts FROM verification_result r "
                                     "JOIN verification_request q USING (request_id)")
            self.assertEqual(set(await cur.fetchall()), {("replay:", None, None, 0)})
            cur = await conn.execute("SELECT count(*) FROM procrastinate_jobs")
            self.assertEqual((await cur.fetchone())[0], 0)

    async def test_a_recheck_uses_saved_answers_only_and_keeps_the_order(self):
        await self.check()
        before = await self.flags()
        asked = len(self.jev.requests)
        outcomes = await self.check(saved_only=True)
        self.assertEqual(len(self.jev.requests), asked)
        self.assertEqual([(o.outcome, o.cost) for o in outcomes], [("flagged", 0), ("flagged", 0)])
        self.assertEqual(await self.flags(), before)

    async def test_a_recheck_without_saved_answers_keeps_no_result(self):
        outcomes = await self.check(saved_only=True)
        self.assertEqual({o.outcome for o in outcomes}, {"kept"})
        self.assertEqual(await checking.results(self.pool, self.bodies), {})

    async def test_checked_disposals_are_not_checked_again(self):
        await self.check(bodies=[self.bodies[0]])
        self.assertEqual(await checking.checked_ids(self.pool, self.bodies), {2001})

    async def test_marks_are_kept_across_a_recheck_and_shown_in_the_report(self):
        await self.check()
        flag = next(f for f in (await self.flags())[2002] if f["code"] == "reused")
        await checking.save_mark(self.pool, 2002, "reused", report.flag_about(flag), "right", "copied photos")
        await checking.save_mark(self.pool, 2001, "disposal_missing", "", "wrong")
        await checking.save_mark(self.pool, 2001, "disposal_missing", "", None)
        await self.check(saved_only=True)
        data = report.build(DAY, self.bodies, await checking.results(self.pool, self.bodies),
                            await checking.marks(self.pool), trips=self.trips)
        card = next(c for c in data["disposals"] if c["id"] == 2002)
        marked = next(f for f in card["flags"] if f["code"] == "reused")
        self.assertEqual((marked["mark"], marked["note"]), ("right", "copied photos"))
        self.assertEqual([r["check"] for r in data["accuracy"] if r["right"]], ["reused"])
        self.assertEqual(data["duplicates"][0]["disposal"], 2002)
        self.assertEqual(data["disposals"][0]["group"], "flagged")
        json.dumps(data, default=str)
        page = report.static_page(data)
        self.assertIn("window.REPORT = ", page)

    async def test_the_database_is_made_on_first_use(self):
        async with await psycopg.AsyncConnection.connect(self.url) as conn:
            cur = await conn.execute("SELECT to_regclass('replay_mark') IS NOT NULL, to_regclass('image_print') IS NOT NULL")
            self.assertEqual(await cur.fetchone(), (True, True))

    async def test_times_are_shown_in_ist(self):
        await self.check(bodies=[self.bodies[0]])
        async with self.pool.connection() as conn:
            cur = await conn.execute("SELECT current_setting('TimeZone'), to_char(made_at, 'OF') FROM verification_result")
            self.assertEqual(await cur.fetchone(), ("Asia/Kolkata", "+05:30"))


if __name__ == "__main__":
    unittest.main()
