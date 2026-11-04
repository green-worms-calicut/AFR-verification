"""A day of gw_sales disposals, exported as JSON, and their images, saved under trips/gw-<day>/.

gw_sales' export (schemaVersion 1) lists disposals as its webhook sends them, with a signedUrl beside each image's
s3Key, valid until signedUrlExpiresAt (100 minutes). `download` saves every image straight away, then the disposals
without their signedUrls (a temporary pass, useless later) in the day's disposals.json: one folder per IST trip date.
An image is found again by its s3Key (`image_path`), the name the checks and flags use.
"""

import json
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from verifier import disposal_check, s3_photos, standards

ROOT = Path(__file__).resolve().parents[2]  # the project folder
TRIPS = ROOT / "trips"  # gitignored: real records
DISPOSALS = "disposals.json"
S3_HOST = ".amazonaws.com"  # signed links are only followed to Amazon S3
WORKERS = 8  # images downloaded at once


class Problem(Exception):
    """Something the person running the tool has to fix, said in plain words (no traceback)."""


def command(*args: str) -> str:
    """The tool's command line for `args`, to show as the next step."""
    return " ".join((".venv/bin/python -m tools.replay_day", *args))


def day_name(day: date) -> str:
    """2026-11-01 -> '1 Nov 2026'."""
    return f"{day.day} {day:%b %Y}"


def day_folder(day: date, trips: Path = TRIPS) -> Path:
    return trips / f"gw-{day.isoformat()}"


def file_name(s3_key: str) -> str:
    """The file an s3Key is saved as: the whole key, so two keys never share a file."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", s3_key.replace("/", "__"))


def image_path(folder: Path, disposal_id: int, s3_key: str) -> Path:
    return folder / str(disposal_id) / file_name(s3_key)


def read_export(path: Path) -> dict:
    """gw_sales' export file, or a Problem saying why it isn't one."""
    try:
        export = json.loads(path.expanduser().read_text())
    except FileNotFoundError:
        raise Problem(f"There is no file {path}.") from None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Problem(f"{path} isn't a JSON file that can be read ({error}).") from None
    if not (isinstance(export, dict) and isinstance(export.get("disposals"), list)):
        raise Problem(f"{path} isn't a gw_sales disposal export: it has no list of disposals.")
    if export.get("schemaVersion") != 1:
        raise Problem(f"{path} is an export of schemaVersion {export.get('schemaVersion')!r}; this tool reads version 1.")
    return export


def utc(text: str | None) -> datetime | None:
    if not text:
        return None
    at = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return at if at.tzinfo else at.replace(tzinfo=UTC)


def without_urls(body: dict) -> dict:
    """A disposal as saved: the export's body without its signed links."""
    return {**body, "evidence": [{k: v for k, v in item.items() if k != "signedUrl"} for item in body.get("evidence") or []]}


def trip_day(body: dict) -> date:
    """The disposal's IST trip date, as the verifier picks it."""
    return disposal_check.disposal(body).day


def fetch(http: httpx.Client, url: str | None) -> tuple[bytes | None, str | None]:
    """An image from its signed link: (data, None), or (None, why it couldn't be saved)."""
    if not url:
        return None, "no link in the export"
    parts = urlsplit(url)
    if parts.scheme != "https" or not (parts.hostname or "").endswith(S3_HOST):
        return None, "the link isn't an Amazon S3 https link"
    try:
        with http.stream("GET", url) as response:
            if response.status_code != 200:
                return None, f"link refused, {response.status_code}" + (" (expired?)" if response.status_code == 403 else "")
            data = b""
            for chunk in response.iter_bytes():
                data += chunk
                if len(data) > s3_photos.MAX_BYTES:
                    break
    except httpx.HTTPError as error:
        return None, f"download failed: {type(error).__name__}"
    if why := s3_photos.problem(data):
        return None, why
    return data, None


@dataclass
class Saved:
    """What `download` did."""

    days: dict[date, int] = field(default_factory=dict)  # disposals saved by trip date
    images: int = 0  # saved now or already there
    failed: list[tuple[int, str, str]] = field(default_factory=list)  # (disposal id, file name, why)
    more_pages: bool = False


def load_saved(folder: Path) -> dict[str, dict]:
    path = folder / DISPOSALS
    return json.loads(path.read_text()) if path.is_file() else {}


def good_file(path: Path) -> bool:
    return path.is_file() and s3_photos.problem(path.read_bytes()) is None


def download(export: dict, trips: Path = TRIPS, http: httpx.Client | None = None, now: datetime | None = None,
             say: Callable[[str], None] = print) -> Saved:
    """Save an export's images and disposals under `trips`. Images already saved aren't downloaded again, so a fresh
    export of the same day fetches only what is still missing."""
    now = now or datetime.now(UTC)
    expires = utc(export.get("signedUrlExpiresAt"))
    if expires and now >= expires:
        raise Problem(f"The image links in this export expired at {expires.astimezone(standards.IST):%H:%M} IST "
                      f"on {day_name(expires.astimezone(standards.IST).date())}.\nExport the day again from gw_sales, "
                      f"then run:\n  {command('download', '~/Downloads/<new file>.json')}")
    disposals = [d for d in export["disposals"] if isinstance(d, dict) and isinstance((d.get("subject") or {}).get("id"), int)]
    if skipped := len(export["disposals"]) - len(disposals):
        say(f"Skipped {skipped} entr{'y' if skipped == 1 else 'ies'} without a disposal id.")
    first = min(map(trip_day, disposals), default=None)
    if expires:
        left = int((expires - now).total_seconds() // 60)
        say(f"Export{' of ' + day_name(first) if first else ''}: {len(disposals)} disposals, image links valid until "
            f"{expires.astimezone(standards.IST):%H:%M} IST ({left} min left).")
    jobs = []  # (disposal id, folder, evidence item)
    for body in disposals:
        folder = day_folder(trip_day(body), trips)
        jobs += [(body["subject"]["id"], folder, item) for item in body.get("evidence") or [] if item.get("s3Key")]

    def save(job) -> str | None:
        disposal_id, folder, item = job
        path = image_path(folder, disposal_id, item["s3Key"])
        if good_file(path):
            return None
        data, why = fetch(http, item.get("signedUrl"))
        if data is None:
            return why
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return None

    own = http is None
    http = http or httpx.Client(timeout=60, follow_redirects=False)
    try:
        with ThreadPoolExecutor(WORKERS) as pool:
            outcomes = list(pool.map(save, jobs))
    finally:
        if own:
            http.close()
    # gw_sales has said an export isn't whole both ways: pageInfo.hasNextPage, and truncated.
    more = bool((export.get("pageInfo") or {}).get("hasNextPage") or export.get("truncated"))
    done, by_folder = Saved(more_pages=more), {}
    for body in disposals:
        disposal_id, day = body["subject"]["id"], trip_day(body)
        mine = [(item, why) for (i, _, item), why in zip(jobs, outcomes) if i == disposal_id]
        bad = [(item.get("fileName") or item["s3Key"], why) for item, why in mine if why]
        done.images += len(mine) - len(bad)
        done.failed += [(disposal_id, name, why) for name, why in bad]
        done.days[day] = done.days.get(day, 0) + 1
        by_folder.setdefault(day_folder(day, trips), {})[str(disposal_id)] = without_urls(body)
        line = f"  {disposal_id}  {len(mine) - len(bad)} image{'' if len(mine) - len(bad) == 1 else 's'} saved"
        if bad:
            line += f", {len(bad)} failed: " + "; ".join(f"{name} ({why})" for name, why in bad)
        say(line)
    for folder, bodies in by_folder.items():
        folder.mkdir(parents=True, exist_ok=True)
        saved = {**load_saved(folder), **bodies}  # a disposal exported again replaces its earlier copy
        (folder / DISPOSALS).write_text(json.dumps(saved, indent=1, sort_keys=True))
    return done


def load_day(day: date, trips: Path = TRIPS) -> list[dict]:
    """A saved day's disposals in the order they were entered (createdAt), as the webhook would have sent them."""
    bodies = list(load_saved(day_folder(day, trips)).values())
    if not bodies:
        raise Problem(f"No disposals are saved for {day_name(day)}. Download the day's export first:\n"
                      f"  {command('download', '~/Downloads/<file>.json')}")
    return sorted(bodies, key=entered)


def entered(body: dict) -> tuple[str, int]:
    """When a disposal was entered, to check them in that order: (createdAt in UTC, id)."""
    at = utc((body.get("subject") or {}).get("createdAt"))
    return (at.isoformat() if at else "9999", body["subject"]["id"])


def fetcher(day: date, trips: Path = TRIPS) -> Callable[[int], Callable[[str], bytes | None]]:
    """For a disposal id, its `fetch` for disposal_check: a saved image's bytes by s3Key, None if it wasn't saved."""
    folder = day_folder(day, trips)

    def of(disposal_id: int) -> Callable[[str], bytes | None]:
        def fetch_saved(key: str) -> bytes | None:
            path = image_path(folder, disposal_id, key)
            return path.read_bytes() if path.is_file() else None
        return fetch_saved
    return of


def saved_days(trips: Path = TRIPS) -> list[date]:
    found = []
    for folder in sorted(trips.glob("gw-*")):
        try:
            found.append(date.fromisoformat(folder.name[3:]))
        except ValueError:
            continue
    return found


def missing_images(day: date, bodies: list[dict], trips: Path = TRIPS) -> list[tuple[int, str]]:
    """(disposal id, file name) of each listed image not saved."""
    folder = day_folder(day, trips)
    return [(b["subject"]["id"], item.get("fileName") or item["s3Key"]) for b in bodies for item in b.get("evidence") or []
            if item.get("s3Key") and not image_path(folder, b["subject"]["id"], item["s3Key"]).is_file()]
