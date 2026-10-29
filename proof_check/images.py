"""Images: reading them upright (by EXIF), sizing them for the API, cutting out a bill's slip or a stamp's
position line, the images of a trip folder by role, and fingerprints to find an image used again (`ImageIndex`).
"""

import hashlib
import io
from dataclasses import dataclass
from pathlib import Path

from PIL import Image as PILImage
from PIL import ImageOps

MIN_SIDE, MAX_SIDE = 280, 1568  # API limits for each side of an image
ROLES = ("loading", "unloading", "bill")  # what each image is for in a trip
SLOT_OF = {"loading": "photo", "unloading": "photo", "bill": "bill"}
ROLE_TITLES = {"loading": "Loading photos", "unloading": "Unloading photos", "bill": "Weigh bills"}
IMAGE_TYPES = (".jpg", ".jpeg", ".png", ".heic")  # the .heic files seen so far were really JPEGs


def fit(img: PILImage.Image) -> PILImage.Image:
    """Scale the longest side to the API maximum (small text reads better larger), padding if too narrow."""
    scale = MAX_SIDE / max(img.size)
    img = img.resize((round(img.width * scale), round(img.height * scale)), PILImage.LANCZOS)
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
    with PILImage.open(io.BytesIO(data)) as original:
        return ImageOps.exif_transpose(original).convert("RGB")


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
                    except OSError:
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


def cut_line(img: PILImage.Image, box) -> PILImage.Image:
    """The stamp's Lat/Long line cut from the full-resolution image, by a Box answer's edges (fractions of
    the width and height), with a little room around it so no digit is cut off.
    """
    w, h = img.size
    tall = (box.bottom - box.top) * h
    edges = (box.left * w - 0.03 * w, box.top * h - 0.6 * tall, box.right * w + 0.03 * w, box.bottom * h + 0.6 * tall)
    left, top, right, bottom = (round(min(max(e, 0), side)) for e, side in zip(edges, (w, h, w, h)))
    return img.crop((left, top, max(right, left + 1), max(bottom, top + 1)))


def cut_slip(img: PILImage.Image, box: dict) -> PILImage.Image:
    """The slip cut from the full-resolution image, by a Box answer's edges (fractions of the width and height),
    with a little room around it.
    """
    w, h = img.size
    wide, tall = (box["right"] - box["left"]) * w, (box["bottom"] - box["top"]) * h
    edges = (box["left"] * w - 0.03 * wide, box["top"] * h - 0.03 * tall,
             box["right"] * w + 0.03 * wide, box["bottom"] * h + 0.03 * tall)
    left, top, right, bottom = (round(min(max(e, 0), side)) for e, side in zip(edges, (w, h, w, h)))
    return img.crop((left, top, max(right, left + 1), max(bottom, top + 1)))
