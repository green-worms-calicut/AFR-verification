"""Images: reading them upright (by EXIF), sizing them for the API, cutting out a bill's slip or a stamp's
position line, the images of a trip folder by role, and fingerprints to find an image used again (`ImageIndex`).
"""

import hashlib
import io
from dataclasses import dataclass
from pathlib import Path

import pillow_heif
from PIL import Image as PILImage
from PIL import ImageOps

pillow_heif.register_heif_opener()  # real HEIC photos, as an iPhone takes them

MIN_SIDE, MAX_SIDE = 280, 1568  # API limits for each side of an image
ROLES = ("loading", "unloading", "bill")  # what each image is for in a trip
SLOT_OF = {"loading": "photo", "unloading": "photo", "bill": "bill"}
ROLE_TITLES = {"loading": "Loading photos", "unloading": "Unloading photos", "bill": "Weigh bills"}
IMAGE_TYPES = (".jpg", ".jpeg", ".png", ".heic")  # real HEIC is read with pillow-heif; some .heic files are JPEGs
# A photo above MAX_PIXELS is read at REDUCED_SIDE on its long side, about today's 12 MP phone photos, so a
# 200 MP one doesn't fill the server's memory. One above MAX_OPEN isn't decoded at all: Pillow only refuses above
# twice its own limit (and only warns below that), so load_bytes refuses it from the size in the file's header.
MAX_PIXELS, REDUCED_SIDE, MAX_OPEN = 50_000_000, 4000, 250_000_000
PILImage.MAX_IMAGE_PIXELS = MAX_OPEN
# What reading a file that isn't a whole image raises: cut off, too large, or not an image at all.
UNREADABLE = (OSError, PILImage.DecompressionBombError, SyntaxError, ValueError)


def fit(img: PILImage.Image) -> PILImage.Image:
    """Scale the longest side to the API maximum (small text reads better larger), padding if too narrow."""
    scale = MAX_SIDE / max(img.size)
    img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), PILImage.LANCZOS)
    if min(img.size) < MIN_SIDE:
        canvas = PILImage.new("RGB", (max(img.width, MIN_SIDE), max(img.height, MIN_SIDE)), "white")
        canvas.paste(img, ((canvas.width - img.width) // 2, (canvas.height - img.height) // 2))
        img = canvas
    return img


@dataclass
class Upload:
    """One image of a trip, as uploaded: its file name and bytes."""

    name: str
    data: bytes


def role_of(name: str) -> str | None:
    """What an image in a trip folder is for, by its file name: collection1.jpg is a loading photo."""
    stem = name.lower()
    if not stem.endswith(IMAGE_TYPES):
        return None
    for prefix, role in (("collection", "loading"), ("loading", "loading"), ("unloading", "unloading"),
                         ("weighbill", "bill"), ("weigh_bill", "bill"), ("bill", "bill")):
        if stem.startswith(prefix):
            return role
    return None


def read_trip_folder(folder: Path) -> dict[str, list[Upload]]:
    """The images in a trip folder by role, in file-name order."""
    found = {role: [] for role in ROLES}
    for path in sorted(folder.iterdir()):
        if path.is_file() and (role := role_of(path.name)):
            found[role].append(Upload(path.name, path.read_bytes()))
    return found


def load_bytes(data: bytes) -> PILImage.Image:
    """The image upright by EXIF, in RGB: a very large one reduced, a 16-bit scan scaled to 8 bits.
    Raises one of UNREADABLE if it isn't a whole image."""
    with PILImage.open(io.BytesIO(data)) as original:
        if original.width * original.height > MAX_OPEN:
            raise PILImage.DecompressionBombError(f"{original.width} x {original.height} is too large to read")
        if original.width * original.height > MAX_PIXELS:
            original.draft("RGB", (REDUCED_SIDE, REDUCED_SIDE))  # JPEGs decode at a fraction of their size
            original.thumbnail((REDUCED_SIDE, REDUCED_SIDE), PILImage.LANCZOS)
        img = ImageOps.exif_transpose(original)
        if img.mode in ("I;16", "I;16B", "I;16L", "I"):  # convert("RGB") would clip every value above 255 to white
            top = 65535 if img.mode.startswith("I;16") else max(img.getextrema()[1], 1)
            img = img.point(lambda v: v * 255 / top).convert("L")
        return img.convert("RGB")


def load(path: Path) -> PILImage.Image:
    return load_bytes(path.read_bytes())


def fingerprints(data: bytes, img: PILImage.Image | None = None) -> dict:
    """An exact fingerprint (md5 of the file) and a look-alike one (a 64-bit difference hash of the picture)."""
    img = img or load_bytes(data)
    px = img.convert("L").resize((9, 8), PILImage.LANCZOS).tobytes()
    bits = sum(1 << i for i in range(64) if px[(i // 8) * 9 + i % 8] > px[(i // 8) * 9 + i % 8 + 1])
    return {"md5": hashlib.md5(data).hexdigest(), "dhash": f"{bits:016x}"}


LOOKS_ALIKE = 3  # at most this many of the 64 look-alike bits differ: a re-saved or resized copy
FEATURELESS = 6  # a look-alike fingerprint with this few bits set, or unset, is a plain image that says nothing


class ImageIndex:
    """Fingerprints of every image in known trips, to find an image used again in another trip."""

    def __init__(self):
        self.items: list[tuple[str, str, str, int]] = []  # (trip, image name, md5, dhash)

    def add(self, trip: str, name: str, prints: dict) -> None:
        self.items.append((trip, name, prints["md5"], int(prints["dhash"], 16)))

    @classmethod
    def of_folders(cls, root: Path) -> "ImageIndex":
        """Index every trip folder under `root`: a folder holding role-named images."""
        index = cls()
        for folder in sorted({p.parent for p in root.rglob("*") if p.is_file() and role_of(p.name)}):
            trip = folder.relative_to(root).as_posix()
            for uploads in read_trip_folder(folder).values():
                for u in uploads:
                    try:
                        index.add(trip, u.name, fingerprints(u.data))
                    except UNREADABLE:
                        continue
        return index

    def matches(self, prints: dict, trip: str | None) -> list[dict]:
        """Images in other trips that are this image: the same file, or one that looks the same."""
        dhash = int(prints["dhash"], 16)
        found = []
        for other, name, md5, h in self.items:
            if other == trip:
                continue
            if md5 == prints["md5"]:
                found.append({"trip": other, "name": name, "exact": True})
            elif FEATURELESS < bin(dhash).count("1") < 64 - FEATURELESS and bin(h ^ dhash).count("1") <= LOOKS_ALIKE:
                found.append({"trip": other, "name": name, "exact": False})
        return found


MIN_CUT = 16  # px: a cut narrower or shorter than this is a box Jev got wrong, so the whole image is read


def crop(img: PILImage.Image, edges: tuple[float, float, float, float]) -> PILImage.Image:
    """`img` cut to (left, top, right, bottom) in pixels, kept within the image; the whole image if that's tiny."""
    w, h = img.size
    left, top, right, bottom = (round(min(max(e, 0), side)) for e, side in zip(edges, (w, h, w, h)))
    if right - left < MIN_CUT or bottom - top < MIN_CUT:
        return img
    return img.crop((left, top, right, bottom))


def cut_line(img: PILImage.Image, box) -> PILImage.Image:
    """The stamp's Lat/Long line cut from the full-resolution image, by a Box answer's edges (fractions of
    the width and height), with a little room around it so no digit is cut off.
    """
    w, h = img.size
    x0, x1, y0, y1 = sorted((box.left, box.right)) + sorted((box.top, box.bottom))  # an inverted box, put right
    tall = (y1 - y0) * h
    return crop(img, (x0 * w - 0.03 * w, y0 * h - 0.6 * tall, x1 * w + 0.03 * w, y1 * h + 0.6 * tall))


def cut_slip(img: PILImage.Image, box: dict) -> PILImage.Image:
    """The slip cut from the full-resolution image, by a Box answer's edges (fractions of the width and height),
    with a little room around it.
    """
    w, h = img.size
    x0, x1, y0, y1 = sorted((box["left"], box["right"])) + sorted((box["top"], box["bottom"]))  # an inverted box, put right
    wide, tall = (x1 - x0) * w, (y1 - y0) * h
    return crop(img, (x0 * w - 0.03 * wide, y0 * h - 0.03 * tall, x1 * w + 0.03 * wide, y1 * h + 0.03 * tall))
