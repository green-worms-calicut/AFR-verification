"""The disposal's photos, read from S3 by s3Key with read-only access (gw_sales' disposal/* and collection-logs/*).

A key that isn't there comes back as None, to be flagged. S3 busy or failing (5xx, throttling, a timeout) raises
S3Unavailable, so the check is retried; any other S3 failure (access) is raised and sent as an error at once.
"""

from botocore.exceptions import ClientError

from proof_check import UNREADABLE, load_bytes

NOT_THERE = {"NoSuchKey", "404", "NotFound"}
BUSY = {"SlowDown", "Throttling", "ThrottlingException", "RequestTimeout", "ServiceUnavailable", "InternalError"}


class S3Unavailable(Exception):
    """S3 was busy or failing for a moment: worth trying again."""
MAX_BYTES = 30 * 1024 * 1024  # a phone photo or scan is far smaller; bigger isn't read
# gw_sales' photo folders: a key elsewhere isn't read, whatever the read-only key's AWS policy allows
# (deploy/s3-read-policy.example.json allows only these two).
PHOTO_PREFIXES = ("disposal/", "collection-logs/")
OUTSIDE = "isn't in one of gw_sales' photo folders"


class Photos:
    def __init__(self, s3, bucket: str):
        self.s3, self.bucket = s3, bucket

    def fetch(self, key: str) -> bytes | None:
        try:
            found = self.s3.get_object(Bucket=self.bucket, Key=key)
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code in NOT_THERE:
                return None
            if code in BUSY or error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) >= 500:
                raise S3Unavailable(f"S3 {code} for {key}") from error
            raise
        with found["Body"] as body:
            return body.read(MAX_BYTES + 1)


def problem(data: bytes | None) -> str | None:
    """Why a fetched file can't be checked as an image, or None."""
    if data is None:
        return "isn't in storage"
    if len(data) > MAX_BYTES:
        return "is too large to read"
    try:
        load_bytes(data)  # decoded in full: verify() alone lets a cut-off JPEG through
    except UNREADABLE:
        return "isn't an image that can be read"
    return None
