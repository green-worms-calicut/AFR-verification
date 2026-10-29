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

import httpx
import procrastinate
from procrastinate import exceptions
from psycopg_pool import AsyncConnectionPool

from verifier import VERSION, checker, signing, store
from verifier.config import Settings

log = logging.getLogger("verifier")
app = procrastinate.App(connector=procrastinate.PsycopgConnector())
NAME = "gw-jev-verifier"
LOST_AFTER = timedelta(minutes=2)  # a request or result this old with no job is queued again


@dataclass
class Service:
    settings: Settings
    pool: AsyncConnectionPool
    http: httpx.AsyncClient


_service: Service | None = None


def service() -> Service:
    if _service is None:
        raise RuntimeError("The service isn't running: open it with jobs.running().")
    return _service


@asynccontextmanager
async def running(settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
    """Open the pool, the queue and the HTTP client (`transport` lets the tests stand in for gw_sales).

    The queue gets a fresh connector each time: one given a pool keeps it after closing.
    """
    global _service
    with app.replace_connector(procrastinate.PsycopgConnector()):
        async with (AsyncConnectionPool(settings.database_url, open=False) as pool,
                    httpx.AsyncClient(transport=transport, timeout=10) as http,
                    app.open_async(pool)):
            _service = Service(settings, pool, http)
            try:
                yield _service
            finally:
                _service = None


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


VERIFY_RETRY = Backoff(60, 600, 30 * 60)  # a checking error (Jev down) is tried for half an hour, then sent as an error
SEND_RETRY = Backoff(30, 3600, 24 * 3600, (Unavailable,))  # the contract's 24 hours of retries


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def make_result(request: dict, outcome: str, flags: list[dict] = (), error: dict | None = None) -> dict:
    """The body posted to the request's callbackUrl."""
    subject = request["subject"]
    result = {"requestId": request["requestId"],
              "subject": {"type": subject["type"], "id": subject["id"], "dataVersion": subject["dataVersion"]},
              "outcome": outcome, "flags": list(flags), "verifier": {"name": NAME, "version": VERSION},
              "completedAt": utc_now()}
    if error:
        result["error"] = error
    return result


async def queue(task: procrastinate.tasks.Task, request_id: str) -> None:
    """Queue `task` for a request, unless it is already waiting to run, and note the job on the request's row."""
    lock = f"{task.name}:{request_id}"
    try:
        job_id = await task.configure(queueing_lock=lock).defer_async(request_id=request_id)
    except exceptions.AlreadyEnqueued:
        waiting = list(await app.job_manager.list_jobs_async(queueing_lock=lock, status="todo"))
        if not waiting:  # it started meanwhile; the sweep catches it if it is lost
            return
        job_id = waiting[0].id
    await store.note_job(service().pool, task.name, request_id, job_id)


async def lost(job_id: int | None) -> bool:
    """Whether a request's job is gone: never noted, removed, or finished without doing its work.

    One waiting, running, or failed (it gave up and said why) isn't lost.
    """
    if job_id is None:
        return True
    found = list(await app.job_manager.list_jobs_async(id=job_id))
    return not found or found[0].status not in ("todo", "doing", "failed")


@app.task(name="verify", queue="verify", pass_context=True, retry=VERIFY_RETRY)
async def verify(context, request_id: str) -> None:
    """Check a saved request and put its result in the outbox, then queue sending it."""
    svc = service()
    if await store.result(svc.pool, request_id) is None:
        request = await store.request(svc.pool, request_id)
        try:
            outcome, flags = await asyncio.to_thread(checker.check, request)
            result = make_result(request, outcome, flags)
        except Exception as error:
            if not VERIFY_RETRY.gives_up(error, context.job):
                log.warning("%s: checking failed, will retry: %r", request_id, error)
                raise
            log.exception("ALERT %s: checking kept failing, sending outcome error", request_id)
            result = make_result(request, "error", error={
                "code": type(error).__name__, "message": "The verifier could not check this disposal."})
        await store.save_result(svc.pool, request_id, result)
        log.info("%s: %s with %d flag(s)", request_id, result["outcome"], len(result["flags"]))
    await queue(send_result, request_id)


@app.task(name="send_result", queue="send", pass_context=True, retry=SEND_RETRY)
async def send_result(context, request_id: str) -> None:
    """Post a result to its callbackUrl, signed; retried while gw_sales is unavailable, for up to 24 hours."""
    svc = service()
    row = await store.result(svc.pool, request_id)
    if row is None or row["sent_at"] or row["gave_up_at"]:
        return
    body = json.dumps(row["body"], separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json", "X-API-Key": svc.settings.api_key,
               **signing.sign(body, svc.settings.callback_secret)}
    try:
        response = await svc.http.post(row["callback_url"], content=body, headers=headers)
    except httpx.TransportError as error:  # timeouts and refused connections
        status, answer = None, f"no answer ({type(error).__name__})"
    else:
        status, answer = response.status_code, response.text[:500]
    if status == 200:
        await store.note_attempt(svc.pool, request_id, status, sent=True)
        log.info("%s: result stored by gw_sales: %s", request_id, answer)
        return
    if status is None or status == 429 or status >= 500:
        error = Unavailable(f"gw_sales {answer if status is None else status}")
        if SEND_RETRY.gives_up(error, context.job):
            await store.note_attempt(svc.pool, request_id, status, gave_up=f"still unavailable after 24 hours: {error}")
            log.error("ALERT %s: gave up sending the result, %s", request_id, error)
        else:
            await store.note_attempt(svc.pool, request_id, status)
            log.warning("%s: %s, will retry", request_id, error)
        raise error
    # 401/403 key or signature, 404 unknown request or disposal, 413 too large, 422 invalid: resending won't help.
    await store.note_attempt(svc.pool, request_id, status, gave_up=f"gw_sales answered {status}: {answer}")
    if status == 404:
        log.warning("%s: gw_sales doesn't know the request or disposal (404): %s", request_id, answer)
    else:
        log.error("ALERT %s: gw_sales refused the result (%s): %s", request_id, status, answer)


@app.periodic(cron="* * * * *", periodic_id="sweep")
@app.task(name="sweep", queue="sweep", queueing_lock="sweep")
async def sweep(timestamp: int) -> None:
    """Retry jobs whose worker died, and queue again requests or results whose job went missing."""
    svc = service()
    for job in await app.job_manager.get_stalled_jobs():
        log.warning("%s: %s job %s stalled, retrying", job.task_kwargs.get("request_id"), job.task_name, job.id)
        await app.job_manager.retry_job(job)
    tasks = {"verify": verify, "send_result": send_result}
    for name, rows in (await store.waiting(svc.pool, datetime.now(UTC) - LOST_AFTER)).items():
        for request_id, job_id in rows:
            if await lost(job_id):
                log.warning("%s: %s job %s lost, queueing it again", request_id, name, job_id)
                await queue(tasks[name], request_id)
