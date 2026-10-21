"""Local test page for check_proof.py: type a vehicle number, add its photo and weigh bill, and see what Jev finds.

The API key stays in this process; the browser only talks to this server.

Usage:
    .venv/bin/python app.py [--port 8765]   # then open the address it prints
"""

import argparse
import base64
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image as PILImage
from PIL import ImageOps, UnidentifiedImageError
from typesafe_sdk import TypeSafeClient, TypeSafeError

import check_proof

HERE = Path(__file__).parent
HOST, PORT = "127.0.0.1", 8765
MAX_BODY = 30 * 1024 * 1024  # two phone photos as base64 JSON are well under this
MODELS = ("jev-latest", "jev-preview")
PAGES = {
    "/": (HERE / "ui.html", "text/html; charset=utf-8"),
    "/samples/bill.png": (HERE / "samples" / "bill.png", "image/png"),
    "/samples/photo.png": (HERE / "samples" / "photo.png", "image/png"),
}


def data_url(img: PILImage.Image, longest: int = 640) -> str:
    img = img.copy()
    img.thumbnail((longest, longest))
    buffer = io.BytesIO()
    img.save(buffer, "JPEG", quality=82)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


class Handler(BaseHTTPRequestHandler):
    def send_json(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/api/status":
            self.send_json(200, {"key_set": bool(check_proof.api_key()), "key_help": check_proof.NO_KEY, "models": MODELS})
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
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_BODY:
            self.send_json(413, {"error": "These images are too large to upload. Use images under 10 MB each."})
            return
        try:
            body = json.loads(self.rfile.read(length))
            model = body.get("model", MODELS[0])
            if model not in MODELS:
                raise ValueError(f"Unknown model {model!r}.")
            number = check_proof.normalize_vehicle_number(body.get("vehicle_number", ""))
            if not number:
                self.send_json(400, {"error": "Type the vehicle number first: 4 to 12 letters and digits, such as KL05AF9341."})
                return
            try:
                window = check_proof.parse_window(body.get("arrival"), body.get("end"))
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
                return
            images = {}
            for slot in check_proof.SLOTS:
                if body.get(slot):
                    raw = base64.b64decode(body[slot].split(",", 1)[-1])
                    with PILImage.open(io.BytesIO(raw)) as original:
                        images[slot] = ImageOps.exif_transpose(original).convert("RGB")
            if not images:
                raise ValueError("Add the vehicle photo, the weigh bill, or both.")
        except UnidentifiedImageError:
            self.send_json(400, {"error": "This file couldn't be opened as an image. Use a JPEG or PNG."})
            return
        except (ValueError, KeyError) as error:
            self.send_json(400, {"error": f"Bad request: {error}"})
            return

        if not (key := check_proof.api_key()):
            self.send_json(503, {"error": check_proof.NO_KEY})
            return
        try:
            with TypeSafeClient(api_key=key, headers=check_proof.HEADERS) as client:
                trip, sent = check_proof.analyze_trip(client, images, number, model, window)
        except TypeSafeError as error:
            self.send_json(502, {"error": f"Jev returned an error: {error}"})
            return
        for slot, img in sent.items():
            trip[slot]["sent_image"] = data_url(img)
            trip[slot]["sent_size"] = list(img.size)
        trip["vehicle_number"] = number
        trip["window"] = window
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
    print("API key: found" if check_proof.api_key() else check_proof.NO_KEY)
    print(f"Open http://{HOST}:{server.server_port} in your browser. Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
