"""Local test page for proof_check.py: type a vehicle number, quantity and the plant's location, tick a Verra project trip, add a
trip's loading photos, unloading photos and weigh bills (or load a trip folder from trips/), and see
what Jev finds.

The API key stays in this process; the browser only talks to this server.

Usage:
    .venv/bin/python -m tools.test_page.server [--port 8765]   # then open the address it prints
"""

import argparse
import base64
import io
import json
import threading
from dataclasses import asdict
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from PIL import Image as PILImage
from typesafe_sdk import TypeSafeClient, TypeSafeError

import proof_check
from tools import check_trip_folders

HERE = Path(__file__).parent
ROOT = HERE.parents[1]  # the project folder
HOST, PORT = "127.0.0.1", 8765
TRIPS = ROOT / "trips"  # trip folders to load on the page, and to find images used again
MAX_BODY = 120 * 1024 * 1024  # a trip's phone photos and slips as base64 JSON are well under this
MODELS = ("jev-latest", "jev-preview")
PAGES = {"/": (HERE / "page.html", "text/html; charset=utf-8")}
_index: proof_check.ImageIndex | None = None
_index_lock = threading.Lock()


def image_index() -> proof_check.ImageIndex | None:
    """Fingerprints of every image under trips/, made once, or None if there is no trips/ folder."""
    global _index
    with _index_lock:
        if _index is None and TRIPS.is_dir():
            _index = proof_check.ImageIndex.of_folders(TRIPS)
        return _index


def data_url(img: PILImage.Image, longest: int = 640) -> str:
    img = img.copy()
    img.thumbnail((longest, longest))
    buffer = io.BytesIO()
    img.save(buffer, "JPEG", quality=82)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def file_url(data: bytes) -> str:
    """A file's bytes as a data URL, typed by its content: the .heic files seen so far are really JPEGs."""
    kind = "image/png" if data.startswith(b"\x89PNG") else "image/jpeg"
    return f"data:{kind};base64," + base64.b64encode(data).decode("ascii")


def trip_folder(trip_id: str) -> Path | None:
    """The folder of a trip id (its path under trips/), or None if it isn't one."""
    folder = (TRIPS / trip_id).resolve()
    if not folder.is_relative_to(TRIPS.resolve()) or not folder.is_dir():
        return None
    return folder if any(proof_check.role_of(p.name) for p in folder.iterdir()) else None


def typed_for(folder: Path) -> tuple[dict | None, str]:
    truth = check_trip_folders.find_truth(folder.parent)
    return check_trip_folders.typed_for(folder, truth.get(folder.name))


def list_trips() -> list[dict]:
    if not TRIPS.is_dir():
        return []
    trips = []
    for folder in check_trip_folders.find_trips(TRIPS):
        typed, source = typed_for(folder)
        counts = {role: len(u) for role, u in proof_check.read_trip_folder(folder).items()}
        trips.append({"id": folder.relative_to(TRIPS).as_posix(), "name": folder.name, "counts": counts,
                      "vehicle_number": (typed or {}).get("vehicle_number"), "typed_from": source})
    return trips


class Handler(BaseHTTPRequestHandler):
    def send_json(self, status: int, body: dict | list) -> None:
        data = json.dumps(body, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/api/status":
            self.send_json(200, {"key_set": bool(proof_check.api_key()), "key_help": proof_check.NO_KEY, "models": MODELS,
                                 "limits": asdict(proof_check.Limits())})
            return
        if self.path == "/api/trips":
            self.send_json(200, list_trips())
            return
        if self.path.startswith("/api/trips/"):
            folder = trip_folder(unquote(self.path.removeprefix("/api/trips/")))
            if not folder:
                self.send_json(404, {"error": "No such trip folder under trips/."})
                return
            typed, source = typed_for(folder)
            images = {role: [{"name": u.name, "data": file_url(u.data)} for u in uploads]
                      for role, uploads in proof_check.read_trip_folder(folder).items()}
            self.send_json(200, {"id": folder.relative_to(TRIPS.resolve()).as_posix(), "images": images,
                                 "typed": typed or {}, "typed_from": source})
            return
        page = PAGES.get(self.path)
        if not page or not page[0].is_file():
            self.send_error(404)
            return
        data = page[0].read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", page[1])
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        if self.path != "/api/check":
            self.send_error(404)
            return
        # Only JSON, and only from a page served here: another website open in the browser can post a plain form,
        # or reach this port through a name of its own (DNS rebinding), and would check trips on your key.
        if self.headers.get_content_type() != "application/json":
            self.send_json(415, {"error": "Send the check as JSON."})
            return
        port = self.server.server_port
        if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            self.send_json(403, {"error": "Open the page at http://127.0.0.1:%d/." % port})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = -1
        if length < 0:
            self.send_json(400, {"error": "Bad request: no length."})
            return
        if length > MAX_BODY:
            self.send_json(413, {"error": "These images are too large to upload together. Use images under 10 MB each."})
            return
        try:
            body = json.loads(self.rfile.read(length))
            model = body.get("model", MODELS[0])
            if model not in MODELS:
                raise ValueError(f"Unknown model {model!r}.")
            number = proof_check.normalize_vehicle_number(body.get("vehicle_number", ""))
            if not number:
                self.send_json(400, {"error": "Type the vehicle number first: 4 to 12 letters and digits, such as KL05AF9341."})
                return
            try:
                quantity = proof_check.parse_quantity(body.get("quantity"))
                plant = proof_check.parse_plant(body.get("plant"))
                verra = proof_check.parse_switch(body.get("verra"), "verra")
                limits = proof_check.parse_limits(body)
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
                return
            uploads = {role: [] for role in proof_check.ROLES}
            for role in proof_check.ROLES:
                for i, item in enumerate(body.get("images", {}).get(role) or []):
                    if not isinstance(item.get("data"), str):
                        raise ValueError("an image's data isn't text")
                    data = base64.b64decode(item["data"].split(",", 1)[-1])
                    proof_check.load_bytes(data)  # fails early if it isn't a whole image
                    uploads[role].append(proof_check.Upload(item.get("name") or f"{role}{i + 1}.jpg", data))
            if not any(uploads.values()):
                raise ValueError("Add the loading photos, unloading photos and weigh bills.")
            trip_id = body.get("trip_id") or None
            if trip_id is not None and (not isinstance(trip_id, str) or "\x00" in trip_id):
                raise ValueError("the trip id isn't a folder name")
        except (OSError, PILImage.DecompressionBombError):  # not an image, cut off, or too large (not ValueError: ours)
            self.send_json(400, {"error": "A file couldn't be opened as a whole image. Use JPEG or PNG (or HEIC)."})
            return
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            self.send_json(400, {"error": f"Bad request: {error}"})
            return

        if not (key := proof_check.api_key()):
            self.send_json(503, {"error": proof_check.NO_KEY})
            return
        try:
            with TypeSafeClient(api_key=key, headers=proof_check.HEADERS, timeout=proof_check.TIMEOUT) as client:
                trip, sent = proof_check.analyze_trip(client, uploads, number, model, quantity, limits,
                                                      image_index(), trip_id, plant=plant, verra=verra,
                                                      around=date.today())
        except (TypeSafeError, proof_check.JevRefused) as error:
            self.send_json(502, {"error": f"Jev returned an error: {error}"})
            return
        for role, imgs in sent.items():
            for r, img in zip(trip["images"][role], imgs):
                r["sent_image"] = data_url(img)
                r["sent_size"] = list(img.size)
        trip.update(vehicle_number=number, quantity=quantity, plant=plant, verra=verra, limits=asdict(limits), trip_id=trip_id)
        self.send_json(200, trip)

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} {fmt % args}")


def serve(port: int) -> ThreadingHTTPServer:
    """Bind to the port asked for, or the next free one if another program has it."""
    for candidate in range(port, port + 20):
        try:
            return ThreadingHTTPServer((HOST, candidate), Handler)
        except OSError:
            print(f"Port {candidate} is in use, trying {candidate + 1}.", flush=True)
    raise SystemExit(f"No free port between {port} and {port + 19}. Choose one with --port.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()

    server = serve(args.port)
    print("API key: found" if proof_check.api_key() else proof_check.NO_KEY)
    print(f"Open http://{HOST}:{server.server_port} in your browser. Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
