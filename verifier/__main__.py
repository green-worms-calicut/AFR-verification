"""Run the verifier. Settings come from the environment (see config.ENV).

Usage:
    .venv/bin/python -m verifier migrate                 # apply the queue's and our migrations not applied yet
    .venv/bin/python -m verifier worker                  # run the jobs
    .venv/bin/python -m verifier web [--host H] [--port P]   # receive gw_sales' requests
"""

import argparse
import asyncio
import logging

import uvicorn

from verifier import jobs, schema
from verifier.config import Settings


async def migrate(settings: Settings) -> None:
    async with jobs.running(settings) as svc:
        applied = await schema.migrate(svc.pool)
    print("Applied: " + ", ".join(applied) if applied else "Up to date.")


async def work(settings: Settings) -> None:
    async with jobs.running(settings):
        await jobs.app.run_worker_async()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m verifier")
    parser.add_argument("command", choices=("migrate", "worker", "web"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.command == "web":
        uvicorn.run("verifier.web:api", host=args.host, port=args.port)
    else:
        settings = Settings.from_env()
        asyncio.run(migrate(settings) if args.command == "migrate" else work(settings))


if __name__ == "__main__":
    main()
