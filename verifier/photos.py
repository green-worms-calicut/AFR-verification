"""The disposal's photos, read from S3 by s3Key with read-only access (gw_sales' disposal/* and collection-logs/*).

A key that isn't there comes back as None, to be flagged; any other S3 failure (access, network) is raised, so
the check is retried and in the end sent as an error.
"""

import io

from botocore.exceptions import ClientError
from PIL import Image as PILImage
from PIL import UnidentifiedImageError

NOT_THERE = {"NoSuchKey", "404", "NotFound"}
MAX_BYTES = 30 * 1024 * 1024  # a phone photo or scan is far smaller; bigger isn't read


class Photos:
    def __init__(self, s3, bucket: str):
        self.s3, self.bucket = s3, bucket

    def fetch(self, key: str) -> bytes | None:
        try:
            found = self.s3.get_object(Bucket=self.bucket, Key=key)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in NOT_THERE:
                return None
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
        with PILImage.open(io.BytesIO(data)) as img:
            img.verify()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        return "isn't an image that can be read"
    return None
