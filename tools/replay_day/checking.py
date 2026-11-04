"""A saved day's disposals checked with the verifier's own code, into the replay database, sending nothing.

Each disposal goes through `disposal_check.check` as a webhook request would, with its images read from the day
folder instead of S3. Disposals are checked in the order they were entered and compared only with disposals entered
before them, as happens live (the verifier never sees a later one). Results are saved exactly as gw_sales would
receive them (`database.save_result`), but no send job is ever queued: `callback_url` is `replay:`.

The replay database (REPLAY_DATABASE_URL, by default verifier_replay on the dev Postgres) is created on first use and
has the verifier's migrations, plus `replay_mark` for the report's marks.
"""

import asyncio
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

import httpx2
import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg_pool import AsyncConnectionPool, PoolTimeout
from typesafe_sdk import RetryPolicy, TypeSafeClient

import proof_check
from tools.replay_day import files
from tools.replay_day.files import Problem
from verifier import compare_disposals, database, disposal_check, entry_checks, migrate, tasks

DATABASE_URL = os.environ.get("REPLAY_DATABASE_URL", "postgresql://verifier@127.0.0.1:5433/verifier_replay")
CALLBACK = "replay:"  # where the export's callbackUrl would be: never posted to
# USD a new image costs to check, low and high: folder runs were about $0.0008 an image (19 Oct 2026), and the
# verifier also reads collection photos' positions and asks bills about the weighbridge.
PER_IMAGE = (0.001, 0.002)
MARKS = """CREATE TABLE IF NOT EXISTS replay_mark (
    disposal_id bigint NOT NULL,
    check_key   text NOT NULL,
    about       text NOT NULL DEFAULT '',  -- the flag's or check's images, so two flags of one check are told apart
    mark        text NOT NULL CHECK (mark IN ('right', 'wrong', 'missed')),
    note        text NOT NULL DEFAULT '',
    marked_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (disposal_id, check_key, about)
)"""


async def open_database(url: str = DATABASE_URL) -> AsyncConnectionPool:
    """The replay database's pool, the database made and migrated first if needed."""
    name = conninfo_to_dict(url).get("dbname")
    try:
        async with await psycopg.AsyncConnection.connect(make_conninfo(url, dbname="postgres"), autocommit=True,
                                                         connect_timeout=5) as conn:
            cur = await conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
            if not await cur.fetchone():
                await conn.execute(f'CREATE DATABASE "{name}"')
        pool = AsyncConnectionPool(url, open=False)
        await pool.open(wait=True, timeout=10)
    except (psycopg.OperationalError, PoolTimeout) as error:
        raise Problem(f"The replay database can't be reached ({str(error).strip().splitlines()[0]}).\n"
                      "Is the dev Postgres running? Start it with: docker start jev-verifier-db") from None
    await migrate.apply(pool)
    async with pool.connection() as conn:
        await conn.execute(MARKS)
    return pool


def request_of(body: dict) -> dict:
    """A saved disposal as a webhook request: its requestId (made from the disposal and its version if the export had
    none) and the stand-in callbackUrl."""
    subject = body["subject"]
    request_id = body.get("requestId") or str(uuid.uuid5(uuid.NAMESPACE_URL, f"gw-disposal:{subject['id']}:{subject.get('dataVersion')}"))
    return {**body, "requestId": request_id, "callbackUrl": CALLBACK, "reason": body.get("reason") or "created",
            "subject": {**subject, "dataVersion": subject.get("dataVersion") or ""}}


def jev_client() -> TypeSafeClient:
    key = proof_check.api_key()
    if not key:
        raise Problem("No TypeSafe API key: set TYPESAFE_API_KEY or keep it in the keyring (README).")
    return TypeSafeClient(api_key=key, headers=proof_check.HEADERS, timeout=proof_check.TIMEOUT)


def saved_only_client() -> TypeSafeClient:
    """A client that never reaches Jev: every request is refused as if Jev were down, so only saved answers are used
    and an image without one is left unanswered."""
    return TypeSafeClient(api_key="saved-answers-only", headers=proof_check.HEADERS, retry=RetryPolicy(max_retries=0),
                          transport=httpx2.MockTransport(lambda request: httpx2.Response(503, json={"detail": "saved answers only"})))


@dataclass
class Outcome:
    disposal_id: int
    vehicle: str
    outcome: str  # passed, flagged, error, or "kept" when a recheck needed answers not saved
    flags: int = 0
    cost: float = 0.0  # what this run cost: new Jev answers only
    why: str = ""


async def entry_order(pool: AsyncConnectionPool) -> dict[int, tuple[str, int]]:
    """Every saved disposal's place in the order of entry (createdAt of its latest request, then id)."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT DISTINCT ON (disposal_id) disposal_id, body FROM verification_request "
                                 "ORDER BY disposal_id, received_at DESC")
        return {i: files.entered(body) for i, body in await cur.fetchall()}


async def replace_result(pool: AsyncConnectionPool, request_id: str, result: dict, analysis: dict | None,
                         cost: float | None) -> None:
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM verification_result WHERE request_id = %s", (request_id,))
    await database.save_result(pool, request_id, result, analysis, cost)


async def check_one(pool: AsyncConnectionPool, body: dict, fetch: Callable[[str], bytes | None], client: TypeSafeClient,
                    verra_project: str | None, order: dict[int, tuple[str, int]], saved_only: bool = False) -> Outcome:
    """Check one disposal and save its result, compared with the disposals entered before it."""
    request = request_of(body)
    request_id, disposal_id = request["requestId"], request["subject"]["id"]
    await database.save_request(pool, request)
    d = disposal_check.disposal(request, verra_project)
    await database.note_disposal(pool, request_id, d.vehicle_key, d.quantity_key, d.day)
    me = order[disposal_id] = files.entered(request)
    earlier = lambda other: order.get(other, ("9999", other)) < me
    others = [compare_disposals.Other(*row) for row in await database.others(pool, disposal_id, d.vehicle_key, d.day)
              if earlier(row[0])] if d.vehicle_key else []
    logs = [x for x in await database.other_logs(pool, disposal_id, [e["id"] for e in entry_checks.logs(request)], d.day)
            if earlier(x[0])]
    loop = asyncio.get_running_loop()
    saved = database.SavedAnswers(pool, loop, request_id, await database.saved_answers(pool, request_id))
    prints = database.PrintHistory(pool, loop, request_id, disposal_id)
    history = lambda found: [c for c in prints(found) if earlier(c.disposal_id)]
    try:
        checked = await asyncio.to_thread(disposal_check.check, request, fetch, client, verra_project, others, saved,
                                          True, history=history, other_logs=logs)
    except Exception as error:
        if saved_only and isinstance(error, disposal_check.JevUnavailable):
            return Outcome(disposal_id, d.vehicle_number, "kept", why="no saved answers: check it with Jev")
        why = f"{type(error).__name__}: {error}"
        result = tasks.make_result(request, "error", error={"code": type(error).__name__,
                                                            "message": "The verifier could not check this disposal."})
        await replace_result(pool, request_id, result, None, None)
        return Outcome(disposal_id, d.vehicle_number, "error", why=why)
    images = [r for results in checked.analysis["images"].values() for r in results]
    if saved_only and checked.analysis["unanswered"]:
        return Outcome(disposal_id, d.vehicle_number, "kept",
                       why=f"{len(checked.analysis['unanswered'])} image(s) have no saved answer: check it with Jev")
    result = tasks.make_result(request, checked.outcome, checked.flags, standard=checked.standard,
                               extractions=checked.extractions)
    await replace_result(pool, request_id, result, checked.analysis, checked.cost_usd)
    return Outcome(disposal_id, d.vehicle_number, checked.outcome, len(checked.flags),
                   sum(r["cost_usd"] for r in images if not r.get("saved")))


async def checked_ids(pool: AsyncConnectionPool, bodies: list[dict]) -> set[int]:
    """The disposals whose current request already has a result other than an error."""
    ids = [request_of(b)["requestId"] for b in bodies]
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT q.disposal_id FROM verification_result r JOIN verification_request q USING (request_id) "
                                 "WHERE r.request_id = ANY(%s::uuid[]) AND r.body->>'outcome' <> 'error'", (ids,))
        return {row[0] for row in await cur.fetchall()}


def estimate(bodies: list[dict]) -> tuple[int, float, float]:
    """Images to send and the cost range, if none were answered before."""
    n = sum(1 for b in bodies for item in b.get("evidence") or [] if item.get("category") in disposal_check.KINDS)
    return n, n * PER_IMAGE[0], n * PER_IMAGE[1]


async def check_day(pool: AsyncConnectionPool, day: date, bodies: list[dict], client: TypeSafeClient,
                    verra_project: str | None, saved_only: bool = False, say: Callable[[str], None] = print,
                    trips=files.TRIPS) -> list[Outcome]:
    """Check `bodies` (a day's disposals in order of entry), saving each result as it is made."""
    order = await entry_order(pool)
    fetch_for = files.fetcher(day, trips)
    done = []
    for n, body in enumerate(bodies, 1):
        out = await check_one(pool, body, fetch_for(body["subject"]["id"]), client, verra_project, order, saved_only)
        done.append(out)
        what = {"flagged": f"flagged ({out.flags} flag{'' if out.flags == 1 else 's'})", "kept": f"not rechecked: {out.why}",
                "error": f"error: {out.why}"}.get(out.outcome, out.outcome)
        say(f"  {n}/{len(bodies)}  {out.disposal_id}  {out.vehicle:<11}  {what:<22}" + (f"  ${out.cost:.3f}" if out.cost else ""))
    return done


async def save_mark(pool: AsyncConnectionPool, disposal_id: int, check_key: str, about: str, mark: str | None,
                    note: str = "") -> None:
    """Set a mark on a flag or a passed check, or clear it (`mark` None)."""
    async with pool.connection() as conn:
        if mark is None:
            await conn.execute("DELETE FROM replay_mark WHERE disposal_id = %s AND check_key = %s AND about = %s",
                               (disposal_id, check_key, about))
            return
        await conn.execute(
            """INSERT INTO replay_mark (disposal_id, check_key, about, mark, note) VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (disposal_id, check_key, about)
               DO UPDATE SET mark = EXCLUDED.mark, note = EXCLUDED.note, marked_at = now()""",
            (disposal_id, check_key, about, mark, note))


async def marks(pool: AsyncConnectionPool) -> dict[tuple[int, str, str], dict]:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT disposal_id, check_key, about, mark, note FROM replay_mark")
        return {(i, k, a): {"mark": m, "note": note} for i, k, a, m, note in await cur.fetchall()}


async def results(pool: AsyncConnectionPool, bodies: list[dict]) -> dict[int, dict]:
    """The saved result and analysis of each disposal's current request, by disposal id."""
    ids = [request_of(b)["requestId"] for b in bodies]
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT q.disposal_id, r.body, r.analysis, r.cost_usd FROM verification_result r "
                                 "JOIN verification_request q USING (request_id) WHERE r.request_id = ANY(%s::uuid[])", (ids,))
        return {i: {"result": body, "analysis": analysis, "cost": float(cost or 0)} for i, body, analysis, cost in await cur.fetchall()}

