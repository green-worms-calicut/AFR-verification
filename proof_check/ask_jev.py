"""Asking Jev about a trip: the API key, sending each image with its questions, turning it upright, cutting
a bill to its slip, reading a stamp's position to four decimals, saving answers for reuse (`answer_key`), and
checking a whole trip in parallel (`analyze_trip`).
"""

import os
import subprocess
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor

from PIL import Image as PILImage
from typesafe_sdk import Image, NoulAnswer, TypeSafeClient

from proof_check.questions import (
    BOX_MODEL, LINE, OUR_COMPANY, ROTATION, SLIP, pick, question_version, questions_for, starts_of
)
from proof_check.images import ImageIndex, ROLES, SLOT_OF, Upload, cut_line, cut_slip, fingerprints, fit, load_bytes
from proof_check.trip_rules import Limits, checks_for, readings_for, reuse_check
from proof_check.verdict import verdict

PRICE_PER_INPUT_TOKEN = 0.042 / 1_000_000  # USD; output tokens are free
HEADERS = {"TypeSafe-Danger-Zone-Images": "true"}  # images are opt-in per request
TIMEOUT = 60.0  # seconds per request; the SDK's 10 s default timed out on large slips in the 19 Oct 2026 run
# Lower than CONFIDENT: a wrong turn only costs a second request, and then gives unsure answers that go to a person.
# The sample bill, photographed sideways, was rightly found turned at 0.56 and was read less surely unturned.
ROTATE = 0.5  # rotate an image and ask again when `orientation` is at least this confident
# Sideways but unsure which way: in the 19 Oct 2026 run Jev put 89-95% on the two sideways options for
# sideways slips, split about 55/40 between them, so no one option reached ROTATE and 33 of 43 sideways
# slips were read sideways. Above this, the likelier way is tried, then checked (see `turn_for`).
SIDEWAYS = 0.7

# The API key lives in the login keyring (GNOME Keyring), stored with KEY_STORE_COMMAND.
KEY_LOOKUP = ["secret-tool", "lookup", "service", "typesafe", "account", "api"]
KEY_STORE_COMMAND = "secret-tool store --label='TypeSafe API key' service typesafe account api"
NO_KEY = f"No API key found. Store it in the keyring by running this in a terminal: {KEY_STORE_COMMAND}"
_keyring_key: str | None = None


def api_key() -> str | None:
    """The TypeSafe API key: TYPESAFE_API_KEY if set, otherwise from the login keyring.

    The key is only ever handed to the client: never printed, written to a file or put in the environment.
    """
    global _keyring_key
    if key := os.environ.get("TYPESAFE_API_KEY"):
        return key
    if not _keyring_key:
        try:  # a locked keyring asks to be unlocked, so allow time for that
            found = subprocess.run(KEY_LOOKUP, capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired):
            return None
        _keyring_key = found.stdout.strip() or None
    return _keyring_key


def ask(client: TypeSafeClient, slot: str, img: PILImage.Image, vehicle_number: str, model: str,
        quantity: str | None = None, place: bool = False, verra: bool = False, weighbridge: str | None = None):
    state = {slot: Image.from_pil(fit(img), format="jpeg"), "vehicle_number": vehicle_number}
    if slot == "bill" and quantity:
        state["quantity_kg"] = quantity
    if verra:
        state["our_company"] = OUR_COMPANY
    if weighbridge:
        state["weighbridge"] = weighbridge
    return client.system_one(state=state, questions=questions_for(slot, quantity, place, verra, bool(weighbridge)),
                             model=model)


def top(answer, n: int = 3) -> list[tuple[str, float]]:
    return sorted(answer.probabilities.items(), key=lambda kv: kv[1], reverse=True)[:n]


def summary(answer) -> dict:
    if isinstance(answer, NoulAnswer):
        return {"type": "noul", "yes": answer.noul}
    return {"type": "choice", "choice": answer.choice, "confidence": answer.confidence, "top": top(answer, 5)}


TURNING = "turn3"  # how images are turned before the answers are kept; changing it means asking again


def answer_key(prints: dict, slot: str, vehicle_number: str, quantity: str | None, model: str, place: bool = False,
               verra: bool = False, weighbridge: str | None = None) -> str:
    """What an image's answers depend on, to save them and reuse them: the image, the questions, what was typed, the model.

    Not the plant's location: it isn't sent, so the same answers serve any plant. The weighbridge entered is only
    added when there is one, so keys saved before it was asked about stay the same.
    """
    parts = [prints["md5"], slot, question_version(slot, place, verra, bool(weighbridge)), TURNING, vehicle_number,
             quantity or "", model]
    return "|".join(parts + ([weighbridge] if weighbridge else []))


def result_from(slot: str, raw: dict, vehicle_number: str, quantity: str | None) -> dict:
    """An image's result from Jev's raw answers: its checks and readings are worked out here, so changing a
    threshold or rule needs no new request.
    """
    tokens = raw["input_tokens"]
    return {**raw, "slot": slot, "checks": checks_for(slot, raw["answers"], vehicle_number, quantity),
            "readings": readings_for(slot, raw["answers"]), "cost_usd": tokens * PRICE_PER_INPUT_TOKEN}


def turn_for(answer, first: bool) -> int:
    """How far to turn an image (counterclockwise) by its `orientation` answer, or 0 to keep it.

    A confident answer is followed. On the first look, an image surely sideways but unsure which way is
    turned the likelier way. Which way is close to a coin flip for Jev, so the next look decides only
    between the two places that turn can have left it: upright, or upside down (then half a turn more).
    In the 19 Oct 2026 run, 6 of 35 slips turned once were left upside down by waiting for ROTATE there.
    """
    p = answer.probabilities
    if not first:
        return 180 if p.get("upside_down", 0) > p.get("upright", 0) else 0
    if answer.choice != "upright" and answer.confidence >= ROTATE:
        return ROTATION[answer.choice]
    if p.get("top_points_left", 0) + p.get("top_points_right", 0) >= SIDEWAYS:
        return ROTATION["top_points_left" if p["top_points_left"] > p["top_points_right"] else "top_points_right"]
    return 0


def find_slip(client: TypeSafeClient, img: PILImage.Image, box_model: str = BOX_MODEL) -> tuple[dict, int]:
    """Where the slip is in a bill image, turned or not: its edges and confidence (a Box answer on BOX_MODEL),
    and the request's input tokens.
    """
    found = client.system_one(state={"bill": Image.from_pil(fit(img), format="jpeg")}, questions={"slip": SLIP},
                              model=box_model)
    box = found.answers["slip"]
    return {**box.box.model_dump(), "confidence": box.confidence}, found.usage.input_tokens or 0


def read_line(client: TypeSafeClient, img: PILImage.Image, raw: dict, model: str, box_model: str = BOX_MODEL) -> dict:
    """Add the position picked to four decimals (`lat_pick`, `long_pick`) to an unloading photo's answers.

    The stamp's Lat/Long line is found with a Box question (on BOX_MODEL), cut out of the upright
    full-resolution photo, and Jev picks each value from the 100 that start with its two-decimal reading
    (`starts_of`). Nothing is added when that reading has an unread part.
    """
    a = raw["answers"]
    lat, long = starts_of(a, "lat"), starts_of(a, "long")
    if not lat or not long:
        return raw
    found = client.system_one(state={"photo": Image.from_pil(fit(img), format="jpeg")}, questions={"line": LINE},
                              model=box_model)
    box = found.answers["line"]
    line = Image.from_pil(fit(cut_line(img, box.box)), format="jpeg")
    questions = {"lat_pick": pick("latitude", "Lat or Latitude", lat), "long_pick": pick("longitude", "Long, Lng or Longitude", long)}
    picked = client.system_one(state={"line": line}, questions=questions, model=model)
    tokens = (found.usage.input_tokens or 0) + (picked.usage.input_tokens or 0)
    return {**raw, "requests": raw["requests"] + 2, "input_tokens": raw["input_tokens"] + tokens,
            "line": {**box.box.model_dump(), "confidence": box.confidence},
            "answers": {**a, **{q: summary(x) for q, x in picked.answers.items()}}}


def analyze(client: TypeSafeClient | None, slot: str, img: PILImage.Image, vehicle_number: str, model: str,
            quantity: str | None = None, cache: dict | None = None, key: str | None = None,
            place: bool = False, verra: bool = False, box_model: str = BOX_MODEL,
            weighbridge: str | None = None) -> tuple[dict, PILImage.Image]:
    """Check one upright-by-EXIF RGB image in its slot. Returns the result and the image as finally sent.

    `quantity` is the typed quantity in kilograms, which a bill's net weight must match. With `place`,
    a photo's GPS stamp position is read too, to four decimals where it can be (`read_line`). With
    `verra`, a bill is asked about the material and the weighbridge's stamp. A bill is first cut to its slip (`find_slip`),
    then turned upright. With `cache` and `key`, saved answers are used instead of asking Jev, and new
    answers are saved. `box_model` answers the Box questions (the verifier pins it).
    """
    if cache is not None and key in cache:
        raw = cache[key]
        result = {**result_from(slot, raw, vehicle_number, quantity), "saved": True}
        if raw.get("slip"):
            img = cut_slip(img, raw["slip"])
        return result, fit(img.rotate(raw["rotated"], expand=True))
    slip, tokens, requests = None, 0, 0
    if slot == "bill":
        slip, tokens = find_slip(client, img, box_model)
        img, requests = cut_slip(img, slip), 1
    response = ask(client, slot, img, vehicle_number, model, quantity, place, verra, weighbridge)
    tokens, requests, rotated = tokens + (response.usage.input_tokens or 0), requests + 1, 0
    turned = response.choices["orientation"]
    looks = [summary(turned)]  # each orientation answer, before each turn
    # At most two turns: the likelier sideways guess, then half a turn if that left it upside down.
    looked = 1
    while looked < 3 and (turn := turn_for(response.choices["orientation"], first=looked == 1)):
        img = img.rotate(turn, expand=True)
        rotated = (rotated + turn) % 360
        response = ask(client, slot, img, vehicle_number, model, quantity, place, verra, weighbridge)
        tokens += response.usage.input_tokens or 0
        requests += 1
        looked += 1
        looks.append(summary(response.choices["orientation"]))
    raw = {"model": response.model, "requests": requests, "orientation": summary(turned), "rotated": rotated, "looks": looks,
           "answers": {q: summary(a) for q, a in response.answers.items()}, "input_tokens": tokens,
           **({"slip": slip} if slip else {})}
    if place:
        raw = read_line(client, img, raw, model, box_model)
    if cache is not None and key:
        cache[key] = raw
    return result_from(slot, raw, vehicle_number, quantity), fit(img)


def analyze_trip(client: TypeSafeClient | None, uploads: dict[str, list[Upload]], vehicle_number: str, model: str,
                 quantity: str | None = None, limits: Limits = Limits(), index: ImageIndex | None = None,
                 trip: str | None = None, cache: dict | None = None,
                 plant: tuple[float, float] | None = None, verra: bool = False,
                 box_model: str = BOX_MODEL, weighbridge: str | None = None,
                 place_also: Iterable[str] = ()) -> tuple[dict, dict]:
    """Check every image of a trip in parallel and decide.

    Returns ({"images": {role: [result]}, "verdict": ...}, {role: [image as sent]}). An image uploaded
    twice in the trip is asked about once. With `index`, images already used in other trips (not
    `trip` itself) are flagged. `cache` holds saved answers (see `analyze`). With `plant`, the
    plant's (latitude, longitude), unloading photos are read for where they were taken and checked
    against it. With `verra` (a Verra project trip), bills are checked for the material and the weighbridge's stamp.
    `box_model` answers the Box questions (finding the slip and the stamp's position line). With `weighbridge`, the
    weighbridge entered, bills are asked whether they are from it. `place_also` names other images to read where
    they were taken (the verifier: collection photos whose collection point is known).
    """
    jobs = []  # (role, upload, image, fingerprints)
    for role in ROLES:
        for u in uploads.get(role, []):
            img = load_bytes(u.data)
            jobs.append((role, u, img, fingerprints(u.data, img)))
    first_of = {}
    for _, u, _, prints in jobs:
        first_of.setdefault(prints["md5"], u.name)
    # Asked where it was taken: an unloading photo, also when the same file is a loading photo too, and any image
    # named in `place_also`.
    placed = {prints["md5"] for role, _, _, prints in jobs if role == "unloading"} if plant else set()
    placed |= {prints["md5"] for _, u, _, prints in jobs if u.name in set(place_also)}

    def run(job):
        role, u, img, prints = job
        slot, place = SLOT_OF[role], prints["md5"] in placed
        asked = verra and slot == "bill"  # the Verra questions are only for bills
        bridge = weighbridge if slot == "bill" else None
        return analyze(client, slot, img, vehicle_number, model, quantity, cache,
                       answer_key(prints, slot, vehicle_number, quantity, model, place, asked, bridge), place, asked,
                       box_model, bridge)

    unique = [j for j in jobs if first_of[j[3]["md5"]] == j[1].name]
    with ThreadPoolExecutor(max(1, min(8, len(unique)))) as pool:
        done = dict(zip((j[3]["md5"] for j in unique), pool.map(run, unique)))
    images, sent = {role: [] for role in ROLES}, {role: [] for role in ROLES}
    for role, u, _, prints in jobs:
        result, img = done[prints["md5"]]
        repeat = first_of[prints["md5"]] != u.name
        images[role].append({**result, "name": u.name, "prints": prints, "requests": 0 if repeat else result["requests"],
                             "input_tokens": 0 if repeat else result["input_tokens"],
                             "cost_usd": 0 if repeat else result["cost_usd"], "same_as": first_of[prints["md5"]] if repeat else None})
        sent[role].append(img)
    extra = [reuse_check(images, index, trip)] if index is not None else []
    return {"images": images, "verdict": verdict(images, vehicle_number, quantity, limits, extra, plant, verra,
                                                 weighbridge)}, sent
