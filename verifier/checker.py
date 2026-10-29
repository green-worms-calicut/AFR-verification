"""What a disposal's request is checked for, and its flags as gw_sales stores them.

For now (build step 1) only whether each kind of image was added; the Jev checks of check_proof come
in with the photos from S3 (step 2).
"""

import logging

import check_proof

log = logging.getLogger("verifier")

# The contract's evidence categories as check_proof's kinds of image. gps_photo and verification are
# never sent; anything else is skipped and logged.
KINDS = {"weighbill": "bill", "collection": "loading", "unloading": "unloading"}
MISSING = {"bill": "No weigh bill was added to the disposal.",
           "loading": "No collection photo was added to the disposal or its collection logs.",
           "unloading": "No unloading photo was added to the disposal."}
# A flag's severity by its part and status: what is wrong with a real trip is high, everything else a recheck,
# but only information when a Verra check is unsure.
SEVERITY = {("real", "problem"): "high", ("real", "review"): "recheck",
            ("docs", "problem"): "recheck", ("docs", "review"): "recheck",
            ("verra", "problem"): "recheck", ("verra", "review"): "info"}
KIND = {"problem": "problem found", "review": "please check"}


def flag(key: str, status: str, message: str, evidence: list[str] = (), target: dict | None = None, **extra) -> dict:
    """One flag: `code` is our check's key, `kind` says whether it is a problem or unsure."""
    part = check_proof.PART_OF[key]
    made = {"code": key, "severity": SEVERITY[part, status], "kind": KIND[status], "message": message,
            "evidence": list(evidence), **extra}
    if target:
        made["target"] = target
    return made


def evidence(request: dict) -> dict[str, list[str]]:
    """The request's s3Keys by kind of image."""
    found = {kind: [] for kind in MISSING}
    for item in request.get("evidence") or []:
        kind = KINDS.get(item.get("category"))
        if kind is None:
            log.info("%s: skipped evidence %s of category %r", request["requestId"], item.get("s3Key"), item.get("category"))
            continue
        found[kind].append(item["s3Key"])
    return found


def check(request: dict) -> tuple[str, list[dict]]:
    """The outcome (passed or flagged) and flags for one request."""
    flags = [flag(f"{kind}_missing", "problem", MISSING[kind]) for kind, keys in evidence(request).items() if not keys]
    return ("flagged" if flags else "passed"), flags
