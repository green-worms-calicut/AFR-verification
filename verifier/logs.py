"""Log lines: one JSON object each on a server, so a log tool can search them by requestId or find every alert;
plain text in a terminal.

The service's messages start with the requestId they are about ("<uuid>: ..."), and one a person must act on
starts with ALERT; both are pulled out into their own fields.
"""

import json
import logging
import re
import sys
from datetime import UTC, datetime

REQUEST_ID = re.compile(r"(?:ALERT )?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


class JsonLines(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        line = {"time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "level": record.levelname, "logger": record.name, "message": message}
        if found := REQUEST_ID.match(message):
            line["requestId"] = found[1]
        if message.startswith("ALERT"):
            line["alert"] = True
        if record.exc_info:
            line["error"] = self.formatException(record.exc_info)
        return json.dumps(line, ensure_ascii=False)


def setup(json_lines: bool | None = None) -> None:
    """Log at INFO to stdout: JSON lines unless `json_lines` is False, or None and stdout is a terminal."""
    handler = logging.StreamHandler(sys.stdout)
    if json_lines if json_lines is not None else not sys.stdout.isatty():
        handler.setFormatter(JsonLines())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
