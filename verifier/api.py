"""The HTTP side: gw_sales posts disposals to check to POST /v1/verifications; GET /health is for monitoring.

A request is answered as soon as it is saved and queued (202, also for a requestId seen before); the
checking and the result come later from the jobs.
"""

import json
import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from verifier import database, signatures, tasks
from verifier.settings import Settings

log = logging.getLogger("verifier")
EVENT = "disposal_log.verification_requested"
SCHEMA_VERSION = 1


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with tasks.running(Settings.from_env()):
        yield


app = FastAPI(title="Jev verifier", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def problem(request: dict, headers, settings: Settings) -> str | None:
    """What makes a correctly signed request unusable, or None."""
    if headers.get("x-gw-event") != EVENT:
        return f"X-GW-Event isn't {EVENT}"
    if not isinstance(request, dict):
        return "body isn't a JSON object"
    try:
        request_id = str(uuid.UUID(str(request.get("requestId"))))
    except ValueError:
        return "requestId isn't a UUID"
    if headers.get("x-gw-delivery") != request_id:
        return "X-GW-Delivery isn't the requestId"
    if request.get("schemaVersion") != SCHEMA_VERSION:
        return f"schemaVersion {request.get('schemaVersion')!r} isn't {SCHEMA_VERSION}"
    subject = request.get("subject")
    if not (isinstance(subject, dict) and subject.get("type") == "disposal_log" and type(subject.get("id")) is int
            and isinstance(subject.get("dataVersion"), str)):
        return "subject needs type disposal_log, a numeric id and a dataVersion"
    if not str(request.get("callbackUrl")).startswith(settings.gw_sales_url.rstrip("/") + "/"):
        return "callbackUrl isn't on gw_sales"
    return None


@app.post("/v1/verifications")
async def receive(request: Request) -> JSONResponse:
    svc = tasks.service()
    body = await request.body()  # the raw bytes are what was signed
    headers = request.headers
    if not signatures.verify(body, headers.get("x-gw-timestamp"), headers.get("x-gw-signature"), svc.settings.webhook_secret):
        log.warning("refused a request (%s): bad or expired signature", headers.get("x-gw-delivery"))
        return JSONResponse({"error": "bad or expired signature"}, status_code=401)
    try:
        payload = json.loads(body)
    except ValueError:
        payload, why = None, "body isn't JSON"
    else:
        why = problem(payload, headers, svc.settings)
    if why:
        log.warning("refused a request (%s): %s", headers.get("x-gw-delivery"), why)
        return JSONResponse({"error": why}, status_code=400)
    request_id = payload["requestId"]
    new = await database.save_request(svc.pool, payload)
    if new:
        try:
            await tasks.queue(tasks.verify, request_id)
        except Exception:  # saved, so the sweep queues it
            log.exception("%s: saved but not queued", request_id)
    log.info("%s: %s disposal %s (%s)", request_id, "received" if new else "already had", payload["subject"]["id"],
             payload.get("reason"))
    return JSONResponse({"accepted": True, "duplicate": not new}, status_code=202)


@app.get("/health")
async def health() -> JSONResponse:
    try:
        async with tasks.service().pool.connection() as conn:
            await conn.execute("SELECT 1")
    except Exception as error:
        log.error("ALERT health: database unreachable: %r", error)
        return JSONResponse({"ok": False, "database": "unreachable"}, status_code=503)
    return JSONResponse({"ok": True})
