"""The queue (Procrastinate, on the same Postgres) and its jobs: check a request, post its result back,
and every minute recover stalled or lost jobs.

`running` opens what the jobs and the receiver share: the settings, one connection pool and one HTTP client.
"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import boto3
import botocore.exceptions
import httpx
import procrastinate
import psycopg
from procrastinate import exceptions
from psycopg_pool import AsyncConnectionPool
from typesafe_sdk import TypeSafeClient

import proof_check
from verifier import VERSION, compare_disposals, database, disposal_check, s3_photos, signatures, standards
from verifier.settings import Settings
from verifier.s3_photos import Photos

log = logging.getLogger("verifier")
app = procrastinate.App(connector=procrastinate.PsycopgConnector())
NAME = "gw-jev-verifier"
LOST_AFTER = timedelta(minutes=2)  # a request or result this old with no job is queued again
MAX_REQUEUES = 3  # times the sweep queues a crashed or lost job again; then a check is sent as an error, a send given up


@dataclass
class Service:
    settings: Settings
    pool: AsyncConnectionPool
    http: httpx.AsyncClient
    photos: Photos
    jev: TypeSafeClient


_service: Service | None = None


def service() -> Service:
    if _service is None:
        raise RuntimeError("The service isn't running: open it with jobs.running().")
    return _service


@asynccontextmanager
async def running(settings: Settings, transport: httpx.AsyncBaseTransport | None = None, jev: TypeSafeClient | None = None):
    """Open the pool, the queue, the HTTP client, S3 and Jev (`transport` and `jev` let the tests stand in for
    gw_sales and Jev).

    The queue gets a fresh connector each time: one given a pool keeps it after closing.
    """
    global _service
    with app.replace_connector(procrastinate.PsycopgConnector()):
        async with (AsyncConnectionPool(settings.database_url, open=False) as pool,
                    httpx.AsyncClient(transport=transport, timeout=10) as http,
                    app.open_async(pool)):
            jev = jev or TypeSafeClient(api_key=settings.typesafe_api_key, headers=proof_check.HEADERS,
                                        timeout=proof_check.TIMEOUT)
            _service = Service(settings, pool, http, Photos(boto3.client("s3"), settings.s3_bucket), jev)
            try:
                yield _service
            finally:
                _service = None
                jev.close()


class Unavailable(Exception):
    """gw_sales didn't store a result: it answered 5xx or 429, timed out or couldn't be reached."""


class Backoff(procrastinate.BaseRetryStrategy):
    """Waits doubling from `first` seconds up to `most`, until `total` seconds of waiting have passed."""

    def __init__(self, first: int, most: int, total: int, retry_exceptions: tuple[type[Exception], ...] = ()):
        self.first, self.most, self.total, self.retry_exceptions = first, most, total, retry_exceptions

    def wait(self, attempts: int) -> int:
        return min(self.first * 2 ** attempts, self.most)

    def get_retry_decision(self, *, exception: BaseException, job) -> procrastinate.RetryDecision | None:
        if self.retry_exceptions and not isinstance(exception, self.retry_exceptions):
            return None
        if sum(self.wait(n) for n in range(job.attempts + 1)) > self.total:
            return None
        return procrastinate.RetryDecision(retry_in={"seconds": self.wait(job.attempts)})

    def gives_up(self, exception: BaseException, job) -> bool:
        return self.get_retry_decision(exception=exception, job=job) is None


# What can change when a check is tried again: Jev busy or down, S3 or the database not reached. Anything else, such
# as our own bug, would fail the same way: it is sent as an error at once, not paid for six times.
RETRYABLE = (disposal_check.JevUnavailable, s3_photos.S3Unavailable, botocore.exceptions.ConnectionError,
             botocore.exceptions.HTTPClientError, psycopg.OperationalError)
VERIFY_RETRY = Backoff(60, 600, 30 * 60, RETRYABLE)  # tried for half an hour (6 tries), then sent as an error
SEND_RETRY = Backoff(30, 3600, 24 * 3600, (Unavailable,))  # the contract's 24 hours of retries


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def make_result(request: dict, outcome: str, flags: list[dict] = (), error: dict | None = None, **extra) -> dict:
    """The body posted to the request's callbackUrl; `extra` are optional fields such as standard and extractions."""
    subject = request["subject"]
    result = {"requestId": request["requestId"],
              "subject": {"type": subject["type"], "id": subject["id"], "dataVersion": subject["dataVersion"]},
              "outcome": outcome, "flags": list(flags), "verifier": {"name": NAME, "version": VERSION},
              "completedAt": utc_now(), **extra}
    if error:
        result["error"] = error
    return result


async def queue(task: procrastinate.tasks.Task, request_id: str) -> None:
    """Queue `task` for a request, unless it is already waiting to run, and note the job on the request's row.

    Checks of one vehicle's disposals run one at a time (a Procrastinate lock), so two duplicates arriving
    together still see each other.
    """
    lock = f"{task.name}:{request_id}"
    options = {"queueing_lock": lock}
    if task is verify and (vehicle := vehicle_of(await database.request(service().pool, request_id))):
        options["lock"] = f"vehicle:{vehicle}"
    try:
        job_id = await task.configure(**options).defer_async(request_id=request_id)
    except exceptions.AlreadyEnqueued:
        waiting = list(await app.job_manager.list_jobs_async(queueing_lock=lock, status="todo"))
        if not waiting:  # it started meanwhile; the sweep catches it if it is lost
            return
        job_id = waiting[0].id
    await database.note_job(service().pool, task.name, request_id, job_id)


def vehicle_of(request: dict | None) -> str | None:
    vehicle = ((request or {}).get("claims") or {}).get("vehicle") or {}
    return proof_check.normalize_vehicle_number(str(vehicle.get("vehicleNumber") or ""))


async def others_of(request_id: str, request: dict, verra_project: str | None) -> list[compare_disposals.Other]:
    """Note what the disposal is compared by, and fetch its vehicle's other disposals within a day."""
    svc = service()
    d = disposal_check.disposal(request, verra_project)
    await database.note_disposal(svc.pool, request_id, d.vehicle_key, d.quantity_key, d.day)
    if not d.vehicle_key:
        return []
    rows = await database.others(svc.pool, request["subject"]["id"], d.vehicle_key, d.day)
    return [compare_disposals.Other(*row) for row in rows]


async def lost(job_id: int | None) -> str | None:
    """Why a request's job won't do its work: "missing" (never noted, removed, or finished without doing it) or
    "failed" (it crashed outside its own error handling: a check or send that fails normally still saves its result
    or notes the give-up). None while it waits, runs, or waits for its next try."""
    if job_id is None:
        return "missing"
    found = list(await app.job_manager.list_jobs_async(id=job_id))
    if not found or found[0].status not in ("todo", "doing", "failed"):
        return "missing"
    return "failed" if found[0].status == "failed" else None


@app.task(name="verify", queue="verify", pass_context=True, retry=VERIFY_RETRY)
async def verify(context, request_id: str) -> None:
    """Check a saved request and put its result in the outbox, then queue sending it."""
    svc = service()
    if await database.result(svc.pool, request_id) is None:
        request = await database.request(svc.pool, request_id)
        analysis = cost = None
        try:
            others = await others_of(request_id, request, svc.settings.verra_project)
            saved = database.SavedAnswers(svc.pool, asyncio.get_running_loop(), request_id,
                                          await database.saved_answers(svc.pool, request_id))
            last_try = VERIFY_RETRY.gives_up(disposal_check.JevUnavailable(), context.job)
            checked = await asyncio.to_thread(disposal_check.check, request, svc.photos.fetch, svc.jev,
                                              svc.settings.verra_project, others, saved, last_try)
            result = make_result(request, checked.outcome, checked.flags, standard=checked.standard,
                                 extractions=checked.extractions)
            analysis, cost = checked.analysis, checked.cost_usd
        except Exception as error:
            if not VERIFY_RETRY.gives_up(error, context.job):
                log.warning("%s: checking failed, will retry: %r", request_id, error)
                raise
            why = "kept failing" if isinstance(error, RETRYABLE) else "failed in a way trying again won't change"
            log.exception("ALERT %s: checking %s, sending outcome error", request_id, why)
            result = make_result(request, "error", error={
                "code": type(error).__name__, "message": "The verifier could not check this disposal."})
        await database.save_result(svc.pool, request_id, result, analysis, cost)
        log.info("%s: %s with %d flag(s), $%.4f", request_id, result["outcome"], len(result["flags"]), cost or 0)
    await queue(send_result, request_id)


@app.task(name="send_result", queue="send", pass_context=True, retry=SEND_RETRY)
async def send_result(context, request_id: str) -> None:
    """Post a result to its callbackUrl, signed; retried while gw_sales is unavailable, for up to 24 hours."""
    svc = service()
    row = await database.result(svc.pool, request_id)
    if row is None or row["sent_at"] or row["gave_up_at"]:
        return
    body = json.dumps(row["body"], separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json", "X-API-Key": svc.settings.api_key,
               **signatures.sign(body, svc.settings.callback_secret)}
    try:
        response = await svc.http.post(row["callback_url"], content=body, headers=headers)
    except httpx.TransportError as error:  # timeouts and refused connections
        status, answer = None, f"no answer ({type(error).__name__})"
    else:
        status, answer = response.status_code, response.text[:500]
    if status is not None and 200 <= status < 300:  # the contract says 200; any success is delivered
        await database.note_attempt(svc.pool, request_id, status, sent=True)
        log.info("%s: result stored by gw_sales: %s", request_id, answer)
        return
    if status is None or status == 429 or status >= 500:
        error = Unavailable(f"gw_sales {answer if status is None else status}")
        if SEND_RETRY.gives_up(error, context.job):
            await database.note_attempt(svc.pool, request_id, status, gave_up=f"still unavailable after 24 hours: {error}")
            log.error("ALERT %s: gave up sending the result, %s", request_id, error)
        else:
            await database.note_attempt(svc.pool, request_id, status)
            log.warning("%s: %s, will retry", request_id, error)
        raise error
    # 401/403 key or signature, 404 unknown request or disposal, 413 too large, 422 invalid: resending won't help.
    await database.note_attempt(svc.pool, request_id, status, gave_up=f"gw_sales answered {status}: {answer}")
    if status == 404:
        log.warning("%s: gw_sales doesn't know the request or disposal (404): %s", request_id, answer)
    else:
        log.error("ALERT %s: gw_sales refused the result (%s): %s", request_id, status, answer)


@app.periodic(cron="35 18 * * *", periodic_id="daily_report")  # 00:05 IST
@app.task(name="daily_report", queue="sweep", queueing_lock="daily_report")
async def daily_report(timestamp: int) -> None:
    """Log the IST day just ended: requests, outcomes, flags by code, Jev's cost, results not delivered. An alert
    when results are waiting to be delivered or were given up."""
    day = datetime.fromtimestamp(timestamp, standards.IST).date() - timedelta(days=1)
    report = await database.day_report(service().pool, day)
    log.info("daily report %s", json.dumps(report))
    if report["resultsWaiting"] or report["resultsGivenUp"] or report["requestsUnchecked"]:
        log.error("ALERT daily report %s: %d results waiting to be delivered, %d given up, %d requests without a result",
                  day, report["resultsWaiting"], report["resultsGivenUp"], report["requestsUnchecked"])


@app.periodic(cron="* * * * *", periodic_id="sweep")
@app.task(name="sweep", queue="sweep", queueing_lock="sweep")
async def sweep(timestamp: int) -> None:
    """Retry jobs whose worker died, and queue again requests or results whose job went missing. Each run is also the
    worker's heartbeat, which the web process watches (api.watch_worker)."""
    svc = service()
    await database.beat(svc.pool)
    for job in await app.job_manager.get_stalled_jobs():
        # Its worker died: perhaps on this very job (out of memory, a native decoder crashing), so it counts as a
        # requeue, and one that keeps killing workers is given up like a crash that repeats.
        request_id = job.task_kwargs.get("request_id")
        if job.task_name in database.JOB_COLUMN and request_id:
            if (await database.note_requeue(svc.pool, job.task_name, request_id) or 0) > MAX_REQUEUES:
                await app.job_manager.finish_job_by_id_async(job.id, procrastinate.jobs.Status.FAILED, delete_job=False)
                await give_up(job.task_name, request_id, "stalled")
                continue
        log.warning("%s: %s job %s stalled, retrying", request_id, job.task_name, job.id)
        await app.job_manager.retry_job(job)
    tasks = {"verify": verify, "send_result": send_result}
    for name, rows in (await database.waiting(svc.pool, datetime.now(UTC) - LOST_AFTER)).items():
        for request_id, job_id, requeues in rows:
            if not (why := await lost(job_id)):
                continue
            if requeues >= MAX_REQUEUES:
                await give_up(name, request_id, why)
                continue
            await database.note_requeue(svc.pool, name, request_id)
            if why == "failed":
                log.error("ALERT %s: %s job %s crashed, queueing it again (%d of %d)", request_id, name, job_id,
                          requeues + 1, MAX_REQUEUES)
            else:
                log.warning("%s: %s job %s lost, queueing it again", request_id, name, job_id)
            await queue(tasks[name], request_id)


async def give_up(name: str, request_id: str, why: str) -> None:
    """After MAX_REQUEUES: a check is sent as outcome error, so gw_sales still hears of it; a send is given up, which
    the daily report counts. If even that fails, the daily report's counts still show it."""
    svc = service()
    log.error("ALERT %s: %s job %s %d times, giving up", request_id, name, why, MAX_REQUEUES)
    try:
        if name == "verify":
            request = await database.request(svc.pool, request_id)
            await database.save_result(svc.pool, request_id, make_result(request, "error", error={
                "code": "VerifierFailed", "message": "The verifier could not check this disposal."}))
            await queue(send_result, request_id)
        else:
            await database.note_attempt(svc.pool, request_id, None, gave_up=f"the send job {why} {MAX_REQUEUES} times")
    except Exception:
        log.exception("ALERT %s: couldn't give up on %s either", request_id, name)
