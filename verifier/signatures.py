"""HMAC-SHA256 signatures both ways: over `timestamp + "." + raw body bytes`, sent as `sha256=<hex>`.

gw_sales signs its requests with the webhook secret and we sign our results with the callback secret.
"""

import hashlib
import hmac
import time

TOLERANCE = 300  # seconds a request's X-GW-Timestamp may be from now


def signature(body: bytes, timestamp: str, secret: str) -> str:
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def sign(body: bytes, secret: str, now: float | None = None) -> dict[str, str]:
    """The timestamp and signature headers for a body we send."""
    timestamp = str(int(time.time() if now is None else now))
    return {"X-GW-Timestamp": timestamp, "X-GW-Signature": signature(body, timestamp, secret)}


def verify(body: bytes, timestamp: str | None, sent: str | None, secret: str, now: float | None = None) -> bool:
    """Whether a request's raw body was signed with `secret` within the last few minutes (either way)."""
    try:
        sent_at = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs((time.time() if now is None else now) - sent_at) > TOLERANCE:
        return False
    try:
        return hmac.compare_digest(signature(body, timestamp, secret), sent or "")
    except TypeError:  # a signature header with non-ASCII characters: not ours
        return False
