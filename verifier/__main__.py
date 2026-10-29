"""Run the verifier. Settings come from the environment (see settings.ENV).

Usage:
    .venv/bin/python -m verifier migrate                 # apply the queue's and our migrations not applied yet
    .venv/bin/python -m verifier worker                  # run the jobs
    .venv/bin/python -m verifier web [--host H] [--port P]   # receive gw_sales' requests
    .venv/bin/python -m verifier report [--day 2026-10-29]    # what happened on an IST day (yesterday by default)

Logs are JSON lines on a server and plain text in a terminal (verifier.logs).
"""

import argparse
import asyncio
import json
from datetime import date, datetime, timedelta

import uvicorn

from verifier import database, logs, migrate, standards, tasks
from verifier.settings import Settings


async def run_migrations(settings: Settings) -> None:
    async with tasks.running(settings) as svc:
        applied = await migrate.apply(svc.pool)
    print("Applied: " + ", ".join(applied) if applied else "Up to date.")


async def run_report(settings: Settings, day: date) -> None:
    async with tasks.running(settings) as svc:
        print(json.dumps(await database.day_report(svc.pool, day), indent=2))


async def run_worker(settings: Settings) -> None:
    async with tasks.running(settings):
        await tasks.app.run_worker_async()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m verifier")
    parser.add_argument("command", choices=("migrate", "worker", "web", "report"))
    parser.add_argument("--day", type=date.fromisoformat, help="report: the IST day, such as 2026-10-29 (yesterday by default)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    logs.setup()
    if args.command == "web":
        uvicorn.run("verifier.api:app", host=args.host, port=args.port, log_config=None)  # our log lines, not uvicorn's
        return
    settings = Settings.from_env()
    if args.command == "report":
        day = args.day or datetime.now(standards.IST).date() - timedelta(days=1)
        asyncio.run(run_report(settings, day))
    else:
        asyncio.run(run_migrations(settings) if args.command == "migrate" else run_worker(settings))


if __name__ == "__main__":
    main()
