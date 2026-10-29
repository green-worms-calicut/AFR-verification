"""The standard a disposal is checked against: the limits in force on its trip's date, and the Jev models.

A change to a limit gets a new entry in STANDARDS from the date it applies, so a trip is always checked by
the rules of its own day, also when it is edited later. Results name the standard they used.
"""

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone

from check_proof import Limits

IST = timezone(timedelta(hours=5, minutes=30), "IST")  # the field team's time; gw_sales sends UTC
# Jev versions, pinned so answers don't change under us when an alias moves; upgrade on purpose after testing.
# 1.14 answered the last real-trip runs (23 and 27 Oct 2026) and is the version Box questions need.
MODEL = "jev-1.14.0"
BOX_MODEL = "jev-1.14.0"


@dataclass(frozen=True)
class Standard:
    version: str
    since: date  # applies to trips on this day (IST) and after, until the next one
    limits: Limits

    def describe(self) -> dict:
        return {"name": "Jev proof check", "version": self.version, "since": self.since.isoformat(),
                "limits": asdict(self.limits), "models": {"questions": MODEL, "box": BOX_MODEL}}


STANDARDS = [Standard("1", date(2026, 10, 1), Limits())]  # oldest first


def for_day(day: date) -> Standard:
    """The standard in force on `day`; the first one for a day before any."""
    return max((s for s in STANDARDS if s.since <= day), key=lambda s: s.since, default=STANDARDS[0])


def ist_day(utc: str | None) -> date | None:
    """The IST date of a time gw_sales sent ("2026-10-27T19:30:00.000Z"), or None if there is none."""
    if not utc:
        return None
    at = datetime.fromisoformat(utc.replace("Z", "+00:00"))
    if at.tzinfo is None:  # the contract says UTC; a time without a zone is taken as UTC too
        at = at.replace(tzinfo=timezone.utc)
    return at.astimezone(IST).date()
