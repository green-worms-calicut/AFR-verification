"""Our tables (made by migrations/): every request gw_sales sent (by requestId), and the outbox of results
to post back.

A result is saved before it is sent, so a crash or a gw_sales outage never loses one. Each row keeps the
job last queued for it, so the sweep can tell when one went missing.
"""

import asyncio
import json
from collections.abc import MutableMapping
from datetime import date, datetime

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

def dumps(value) -> str:
    """JSON for proof_check's results, which hold the odd date."""
    return json.dumps(value, default=str)


# The column holding the job last queued for a request, by task (see migrations/001).
SWEEP_DAYS = 7  # how far back the sweep looks for work left undone; older is counted by the daily report instead
JOB_COLUMN = {"verify": ("verification_request", "verify_job"), "send_result": ("verification_result", "send_job")}


async def save_request(pool: AsyncConnectionPool, request: dict) -> bool:
    """Save a request; False if its requestId was already saved (gw_sales never changes one's content)."""
    subject = request["subject"]
    async with pool.connection() as conn:
        cur = await conn.execute(
            """INSERT INTO verification_request (request_id, disposal_id, data_version, reason, callback_url, body)
               VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (request_id) DO NOTHING""",
            (request["requestId"], subject["id"], subject["dataVersion"], request.get("reason") or "created",
             request["callbackUrl"], Jsonb(request)))
        return cur.rowcount == 1


async def request(pool: AsyncConnectionPool, request_id: str) -> dict | None:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT body FROM verification_request WHERE request_id = %s", (request_id,))
        row = await cur.fetchone()
        return row[0] if row else None


async def save_result(pool: AsyncConnectionPool, request_id: str, result: dict, analysis: dict | None = None,
                      cost_usd: float | None = None) -> None:
    """Put a result in the outbox, with proof_check's whole `analysis` and what it cost; a result already there
    for the request is kept."""
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO verification_result (request_id, body, analysis, cost_usd) VALUES (%s, %s, %s, %s) "
                           "ON CONFLICT (request_id) DO NOTHING",
                           (request_id, Jsonb(result), None if analysis is None else Jsonb(analysis, dumps=dumps), cost_usd))


async def result(pool: AsyncConnectionPool, request_id: str) -> dict | None:
    """A request's outbox row: its `body` and how sending it went."""
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT r.*, q.callback_url FROM verification_result r "
                          "JOIN verification_request q USING (request_id) WHERE request_id = %s", (request_id,))
        return await cur.fetchone()


async def note_attempt(pool: AsyncConnectionPool, request_id: str, status: int | None, sent: bool = False,
                       gave_up: str | None = None) -> None:
    """Record one try at posting a result: gw_sales' status (None if it didn't answer), and whether it is done."""
    async with pool.connection() as conn:
        await conn.execute(
            """UPDATE verification_result SET attempts = attempts + 1, last_status = %s,
                      sent_at = CASE WHEN %s THEN now() END,
                      gave_up_at = CASE WHEN %s::text IS NOT NULL THEN now() END, gave_up_why = %s
               WHERE request_id = %s""", (status, sent, gave_up, gave_up, request_id))


async def note_job(pool: AsyncConnectionPool, task: str, request_id: str, job_id: int) -> None:
    table, column = JOB_COLUMN[task]
    async with pool.connection() as conn:
        await conn.execute(f"UPDATE {table} SET {column} = %s WHERE request_id = %s", (job_id, request_id))


async def waiting(pool: AsyncConnectionPool, before: datetime) -> dict[str, list[tuple[str, int | None, int]]]:
    """By task, the (requestId, job, times queued again) still waiting for it since before `before`, within the last
    SWEEP_DAYS: requests with no result (verify) and results neither sent nor given up (send_result)."""
    since = f"now() - interval '{SWEEP_DAYS} days'"
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT request_id::text, verify_job, requeues FROM verification_request q "
            f"WHERE received_at < %s AND received_at > {since} "
            "AND NOT EXISTS (SELECT 1 FROM verification_result r WHERE r.request_id = q.request_id)", (before,))
        unchecked = await cur.fetchall()
        cur = await conn.execute(
            "SELECT request_id::text, send_job, requeues FROM verification_result "
            f"WHERE made_at < %s AND made_at > {since} AND sent_at IS NULL AND gave_up_at IS NULL", (before,))
        unsent = await cur.fetchall()
    return {"verify": unchecked, "send_result": unsent}


async def saved_answers(pool: AsyncConnectionPool, request_id: str) -> dict[str, dict]:
    """Jev's answers already saved for a request, by answer_key (migrations/004)."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT answer_key, raw FROM jev_answer WHERE request_id = %s", (request_id,))
        return dict(await cur.fetchall())


class SavedAnswers(MutableMapping):
    """A request's Jev answers as proof_check's `cache`, for the thread that runs analyze_trip: read from `saved`,
    and each new one written to Postgres straight away (on `loop`, the worker's), so a retry after a crash halfway
    asks only the images not answered yet."""

    def __init__(self, pool: AsyncConnectionPool, loop: asyncio.AbstractEventLoop, request_id: str, saved: dict[str, dict]):
        self.pool, self.loop, self.request_id, self.saved = pool, loop, request_id, saved

    def __getitem__(self, key: str) -> dict:
        return self.saved[key]

    def __setitem__(self, key: str, raw: dict) -> None:
        self.saved[key] = raw
        asyncio.run_coroutine_threadsafe(self.save(key, raw), self.loop).result()

    async def save(self, key: str, raw: dict) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("INSERT INTO jev_answer (request_id, answer_key, raw) VALUES (%s, %s, %s) "
                               "ON CONFLICT DO NOTHING", (self.request_id, key, Jsonb(raw, dumps=dumps)))

    def __delitem__(self, key: str) -> None:
        raise TypeError("saved answers are kept")

    def __iter__(self):
        return iter(self.saved)

    def __len__(self) -> int:
        return len(self.saved)


async def note_requeue(pool: AsyncConnectionPool, task: str, request_id: str) -> int | None:
    """Count one more time the sweep queued a request's `task` again; the new count (None without its row)."""
    table, _ = JOB_COLUMN[task]
    async with pool.connection() as conn:
        cur = await conn.execute(f"UPDATE {table} SET requeues = requeues + 1 WHERE request_id = %s RETURNING requeues",
                                 (request_id,))
        row = await cur.fetchone()
        return row[0] if row else None


async def note_disposal(pool: AsyncConnectionPool, request_id: str, vehicle: str | None, quantity: str | None,
                        day: date) -> None:
    """Keep what a request's disposal is compared with others by (migrations/003)."""
    async with pool.connection() as conn:
        await conn.execute("UPDATE verification_request SET vehicle_number = %s, quantity = %s, trip_day = %s "
                           "WHERE request_id = %s", (vehicle, quantity, day, request_id))


async def others(pool: AsyncConnectionPool, disposal_id: int, vehicle: str, day: date) -> list[tuple[int, dict]]:
    """The other disposals of `vehicle` with a trip within a day of `day`, each as last edited: (disposal id,
    proof_check's result). A disposal's earlier requests are its own edits, never others. One whose latest request
    isn't analysed (still being checked, or it ended in an error) is left out: its earlier photos may have been
    changed, and one still being checked compares itself with this one when it runs (one vehicle at a time)."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT disposal_id, analysis FROM (
                   SELECT DISTINCT ON (q.disposal_id) q.disposal_id, q.vehicle_number, q.trip_day, r.analysis
                   FROM verification_request q LEFT JOIN verification_result r USING (request_id)
                   WHERE q.disposal_id <> %(disposal)s AND q.disposal_id IN (
                       SELECT disposal_id FROM verification_request
                       WHERE vehicle_number = %(vehicle)s AND trip_day BETWEEN %(day)s::date - 1 AND %(day)s::date + 1)
                   ORDER BY q.disposal_id, q.received_at DESC) latest
               WHERE vehicle_number = %(vehicle)s AND trip_day BETWEEN %(day)s::date - 1 AND %(day)s::date + 1
                 AND analysis IS NOT NULL""", {"disposal": disposal_id, "vehicle": vehicle, "day": day})
        return await cur.fetchall()


async def beat(pool: AsyncConnectionPool) -> None:
    """The worker is alive (migrations/007)."""
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO worker_heartbeat (id, beat_at) VALUES (1, now()) "
                           "ON CONFLICT (id) DO UPDATE SET beat_at = now()")


async def last_beat(pool: AsyncConnectionPool) -> datetime | None:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT beat_at FROM worker_heartbeat")
        row = await cur.fetchone()
        return row[0] if row else None


async def day_report(pool: AsyncConnectionPool, day: date) -> dict:
    """What happened on one IST day: requests received, results made by outcome and flags by code, what Jev cost, and
    results not delivered yet and requests without a result (at any time), for the daily summary and
    `python -m verifier report`."""
    on_day = "(%s AT TIME ZONE 'Asia/Kolkata')::date = %%s"
    async with pool.connection() as conn:
        cur = await conn.execute(f"SELECT reason, count(*) FROM verification_request WHERE {on_day % 'received_at'} "
                                 "GROUP BY reason", (day,))
        received = dict(await cur.fetchall())
        cur = await conn.execute(f"SELECT body->>'outcome', count(*), coalesce(sum(cost_usd), 0) FROM verification_result "
                                 f"WHERE {on_day % 'made_at'} GROUP BY 1", (day,))
        outcomes = {o: (n, float(c)) for o, n, c in await cur.fetchall()}
        cur = await conn.execute(f"SELECT f->>'code', count(*) FROM verification_result, jsonb_array_elements(body->'flags') f "
                                 f"WHERE {on_day % 'made_at'} GROUP BY 1 ORDER BY 2 DESC", (day,))
        flags = dict(await cur.fetchall())
        cur = await conn.execute("SELECT count(*) FILTER (WHERE sent_at IS NULL AND gave_up_at IS NULL), "
                                 f"count(*) FILTER (WHERE {on_day % 'gave_up_at'}) FROM verification_result", (day,))
        waiting, gave_up = await cur.fetchone()
        cur = await conn.execute("SELECT count(*) FROM verification_request q WHERE NOT EXISTS "
                                 "(SELECT 1 FROM verification_result r WHERE r.request_id = q.request_id)")
        (unchecked,) = await cur.fetchone()
    return {"day": day.isoformat(), "received": received, "outcomes": {o: n for o, (n, _) in outcomes.items()},
            "flags": flags, "jevCostUsd": round(sum(c for _, c in outcomes.values()), 4),
            "resultsWaiting": waiting, "resultsGivenUp": gave_up, "requestsUnchecked": unchecked}
