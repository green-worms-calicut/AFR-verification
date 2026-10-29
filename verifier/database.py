"""Our tables (made by migrations/): every request gw_sales sent (by requestId), and the outbox of results
to post back.

A result is saved before it is sent, so a crash or a gw_sales outage never loses one. Each row keeps the
job last queued for it, so the sweep can tell when one went missing.
"""

import json
from datetime import date, datetime

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

def dumps(value) -> str:
    """JSON for proof_check's results, which hold the odd date."""
    return json.dumps(value, default=str)


# The column holding the job last queued for a request, by task (see migrations/001).
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


async def waiting(pool: AsyncConnectionPool, before: datetime) -> dict[str, list[tuple[str, int | None]]]:
    """By task, the (requestId, job) still waiting for it since before `before`: requests with no result
    (verify) and results neither sent nor given up (send_result)."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT request_id::text, verify_job FROM verification_request q WHERE received_at < %s "
            "AND NOT EXISTS (SELECT 1 FROM verification_result r WHERE r.request_id = q.request_id)", (before,))
        unchecked = await cur.fetchall()
        cur = await conn.execute(
            "SELECT request_id::text, send_job FROM verification_result WHERE made_at < %s "
            "AND sent_at IS NULL AND gave_up_at IS NULL", (before,))
        unsent = await cur.fetchall()
    return {"verify": unchecked, "send_result": unsent}


async def note_disposal(pool: AsyncConnectionPool, request_id: str, vehicle: str | None, quantity: str | None,
                        day: date) -> None:
    """Keep what a request's disposal is compared with others by (migrations/003)."""
    async with pool.connection() as conn:
        await conn.execute("UPDATE verification_request SET vehicle_number = %s, quantity = %s, trip_day = %s "
                           "WHERE request_id = %s", (vehicle, quantity, day, request_id))


async def others(pool: AsyncConnectionPool, disposal_id: int, vehicle: str, day: date) -> list[tuple[int, dict]]:
    """The other disposals of `vehicle` with a trip within a day of `day`, each as last checked:
    (disposal id, proof_check's result). A disposal's earlier requests are its own edits, never others."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT DISTINCT ON (q.disposal_id) q.disposal_id, r.analysis
               FROM verification_request q JOIN verification_result r USING (request_id)
               WHERE q.vehicle_number = %s AND q.trip_day BETWEEN %s::date - 1 AND %s::date + 1
                 AND q.disposal_id <> %s AND r.analysis IS NOT NULL
               ORDER BY q.disposal_id, q.received_at DESC""", (vehicle, day, day, disposal_id))
        return await cur.fetchall()


async def day_report(pool: AsyncConnectionPool, day: date) -> dict:
    """What happened on one IST day: requests received, results made by outcome and flags by code, what Jev cost, and
    results not delivered yet (at any time), for the daily summary and `python -m verifier report`."""
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
    return {"day": day.isoformat(), "received": received, "outcomes": {o: n for o, (n, _) in outcomes.items()},
            "flags": flags, "jevCostUsd": round(sum(c for _, c in outcomes.values()), 4),
            "resultsWaiting": waiting, "resultsGivenUp": gave_up}
