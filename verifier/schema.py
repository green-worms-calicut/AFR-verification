"""Database migrations: ours (verifier/migrations/NNN_name.sql) and Procrastinate's, each applied once,
in order, and recorded in verifier_migration.

A new database gets Procrastinate's whole schema and has its migrations so far recorded as applied. After
upgrading Procrastinate, stop the workers and run `migrate` again: its new migrations are applied.
"""

import logging
from importlib.metadata import version
from pathlib import Path

from psycopg_pool import AsyncConnectionPool

from verifier import jobs

log = logging.getLogger("verifier")
OURS = Path(__file__).parent / "migrations"
LOCK = 7_291_301  # pg advisory lock: one migrate at a time


def procrastinate_migrations() -> list[Path]:
    return sorted(Path(jobs.app.schema_manager.get_migrations_path()).glob("*.sql"))


def our_migrations() -> list[Path]:
    return sorted(OURS.glob("[0-9][0-9][0-9]_*.sql"))


async def migrate(pool: AsyncConnectionPool) -> list[str]:
    """Apply every migration not applied yet; returns their names."""
    applied_now = []
    async with pool.connection() as conn:
        await conn.execute("SELECT pg_advisory_lock(%s)", (LOCK,))
        try:
            await conn.execute("CREATE TABLE IF NOT EXISTS verifier_migration "
                               "(name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
            await conn.commit()
            cur = await conn.execute("SELECT name FROM verifier_migration")
            applied = {row[0] for row in await cur.fetchall()}
            cur = await conn.execute("SELECT to_regclass('procrastinate_jobs') IS NOT NULL")
            installed = (await cur.fetchone())[0]
            ours = [("verifier/" + p.name, p) for p in our_migrations()]
            theirs = [("procrastinate/" + p.name, p) for p in procrastinate_migrations()]

            if not installed:
                await conn.execute(jobs.app.schema_manager.get_schema())
                for name, _ in theirs:
                    await conn.execute("INSERT INTO verifier_migration (name) VALUES (%s)", (name,))
                await conn.commit()
                applied |= {name for name, _ in theirs}
                applied_now.append(f"procrastinate {version('procrastinate')} schema")
            elif not any(name.startswith("procrastinate/") for name in applied):
                raise SystemExit("Procrastinate's tables exist but no migrations of it are recorded in "
                                 "verifier_migration: this database wasn't set up by `migrate`.")

            for name, path in theirs + ours:
                if name in applied:
                    continue
                await conn.execute(path.read_text())
                await conn.execute("INSERT INTO verifier_migration (name) VALUES (%s)", (name,))
                await conn.commit()  # each migration with its record, or neither
                applied_now.append(name)
                log.info("applied migration %s", name)
        finally:
            await conn.rollback()
            await conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK,))
    return applied_now
