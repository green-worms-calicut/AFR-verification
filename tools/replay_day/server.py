"""The report page, served on this laptop only, with the marks saved as they are clicked.

The database is reached from one event loop in a background thread; each page request waits for its answer. The
Jev key isn't needed here and never reaches the page.
"""

import asyncio
import json
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from tools.replay_day import checking, files, report

HOST = "127.0.0.1"
MAX_BODY = 64 * 1024  # a mark and its note
MARKS = ("right", "wrong", "missed")


class Report:
    """What the page asks for: the report's data and marks, from the replay database on its own loop."""

    def __init__(self, day: date, margin: float, sample: int, trips: Path = files.TRIPS, url: str = checking.DATABASE_URL):
        self.day, self.margin, self.sample, self.trips = day, margin, sample, trips
        self.bodies = files.load_day(day, trips)
        self.ids = {b["subject"]["id"] for b in self.bodies}
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.pool = self.run(checking.open_database(url))

    def run(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result()

    def data(self) -> dict:
        saved = self.run(checking.results(self.pool, self.bodies))
        return report.build(self.day, self.bodies, saved, self.run(checking.marks(self.pool)), self.margin, self.sample,
                            self.trips)

    def mark(self, body: dict) -> str | None:
        """Save a mark from the page; why it can't be, or None."""
        disposal_id, key, about = body.get("disposal"), body.get("check"), body.get("about", "")
        if disposal_id not in self.ids or not isinstance(key, str) or not isinstance(about, str):
            return "unknown disposal or check"
        mark = body.get("mark")
        if mark is not None and mark not in MARKS:
            return "unknown mark"
        self.run(checking.save_mark(self.pool, disposal_id, key, about, mark, str(body.get("note") or "")[:2000]))
        return None

    def image(self, disposal_id: str, name: str, small: bool, turn: int = 0) -> bytes | None:
        if not disposal_id.isdigit() or int(disposal_id) not in self.ids or "/" in name or name.startswith("."):
            return None
        path = files.day_folder(self.day, self.trips) / disposal_id / name
        if not path.is_file():
            return None
        if small:
            return report.thumbnail(path, turn=turn)
        return report.thumbnail(path, 2400, turn) if turn else path.read_bytes()  # turned as Jev read it

    def close(self) -> None:
        self.run(self.pool.close())
        self.loop.call_soon_threadsafe(self.loop.stop)


def handler(source: Report) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def send(self, status: int, body: bytes, kind: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, status: int, value) -> None:
            self.send(status, json.dumps(value, default=str).encode(), "application/json")

        def do_GET(self) -> None:
            parts = urlsplit(self.path)
            if parts.path == "/":
                self.send(200, report.PAGE.read_bytes(), "text/html; charset=utf-8")
            elif parts.path == "/data":
                self.send_json(200, source.data())
            elif parts.path.startswith(("/thumb/", "/image/")):
                _, what, disposal_id, name = (parts.path.split("/", 3) + [""] * 4)[:4]
                query = parse_qs(parts.query)
                turn = int(query["turn"][0]) if query.get("turn", [""])[0] in {str(t) for t in report.TURNS} else 0
                data = source.image(disposal_id, unquote(name), what == "thumb" or "small" in query, turn)
                if data is None:
                    self.send(404, b"not found", "text/plain")
                else:
                    self.send(200, data, "image/png" if data.startswith(b"\x89PNG") else "image/jpeg")
            else:
                self.send(404, b"not found", "text/plain")

        def do_POST(self) -> None:
            if urlsplit(self.path).path != "/mark":
                return self.send(404, b"not found", "text/plain")
            if self.headers.get("Origin") not in (None, f"http://{HOST}:{self.server.server_port}"):
                return self.send_json(403, {"error": "marks are only taken from this page"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self.send_json(413, {"error": "too large"})
            try:
                body = json.loads(self.rfile.read(length))
            except json.JSONDecodeError:
                return self.send_json(400, {"error": "not JSON"})
            why = source.mark(body) if isinstance(body, dict) else "not an object"
            self.send_json(400 if why else 200, {"error": why} if why else {"ok": True})

        def log_message(self, *args) -> None:  # quiet: the terminal shows only what the tool says
            pass

    return Handler


def serve(source: Report, port: int) -> None:
    server = ThreadingHTTPServer((HOST, port), handler(source))
    print(f"Report for {files.day_name(source.day)} open at http://{HOST}:{server.server_port}  (only on this laptop). "
          "Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped. Marks are saved.")
    finally:
        server.server_close()
        source.close()
