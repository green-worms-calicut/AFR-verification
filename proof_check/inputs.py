"""What people type in, checked and made uniform: the vehicle number, the quantity in kg, the plant's
location, the limits and the Verra switch.
"""

import re
from dataclasses import fields

from proof_check.trip_rules import LIMIT_NAMES, LIMIT_UNITS, Limits


INDIA = ((6.0, 37.5), (68.0, 97.5))  # latitude and longitude ranges a plant can be in (catches them swapped)


FULL_NUMBER = r"[A-Z]{2}\d{1,2}[A-Z]{1,3}\d{1,4}"  # state, district, series and number, as KL05AF9341


def normalize_vehicle_number(text: str) -> str | None:
    """'kl 05 af-9341' -> 'KL05AF9341'. None if it doesn't look like a vehicle number.

    A note after the number is dropped ('KL05AF9341/T', 'KL05AF9341 (hired)'), and a one-digit district gets its 0
    ('KL 5 AF 9341' -> 'KL05AF9341'), so the same vehicle entered either way is compared with its other disposals.
    """
    parts = re.split(r"[/(]", text or "", maxsplit=1)
    number = re.sub(r"[\s.\-]", "", parts[0]).upper()
    if len(parts) > 1 and not re.fullmatch(FULL_NUMBER, number):  # a note only after a whole number, never mid-number
        return None
    if one_digit := re.fullmatch(r"([A-Z]{2})(\d)([A-Z]{1,3}\d{1,4})", number):
        number = f"{one_digit[1]}0{one_digit[2]}{one_digit[3]}"
    return number if re.fullmatch(r"[A-Z0-9]{4,12}", number) else None


def parse_limits(typed: dict) -> Limits:
    """Limits from typed whole numbers, such as {"clock_slack": "10"}. A blank or missing one keeps its default."""
    values = {}
    for f in fields(Limits):
        value = typed.get(f.name)
        text = "" if value is None else str(value).strip()  # a typed 0 is kept
        if not text:
            continue
        if not re.fullmatch(r"\d{1,4}", text):
            raise ValueError(f"Type {LIMIT_NAMES[f.name]} as whole {LIMIT_UNITS[f.name]}, such as {f.default}.")
        values[f.name] = int(text)
    return Limits(**values)


def parse_quantity(text: str | None) -> str | None:
    """'2,570 kg' -> '2570', '480.50' -> '480.5'. None if nothing was typed.

    The quantity is in kilograms, as weighbridge bills print it.
    """
    text = str(text or "").strip()
    if not text:
        return None
    if re.search(r"\d,\d{1,2}\s*(?:kgs?)?\.?$", text, re.IGNORECASE):  # 2570,5 ends in a decimal comma; 1,25,000 doesn't
        raise ValueError("Type the quantity with a decimal point, not a comma, such as 2570.5.")
    found = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:kg|kgs)?\.?", text.replace(",", ""), re.IGNORECASE)
    if not found or float(found[1]) <= 0:
        raise ValueError("Type the quantity in kilograms as a number, such as 480.")
    number = found[1].lstrip("0") or "0"
    if "." in number:
        number = number.rstrip("0").rstrip(".")
    return "0" + number if number.startswith(".") else number


def parse_plant(value) -> tuple[float, float] | None:
    """'11.4404, 75.9420' or [11.4404, 75.942] -> (11.4404, 75.942): the plant's latitude and longitude.

    In decimal degrees north and east, as Google Maps copies a place. None if nothing was given.
    """
    text = ", ".join(map(str, value)) if isinstance(value, (list, tuple)) else str(value or "")
    text = text.replace("°", "").strip()
    if not text:
        return None
    found = re.fullmatch(r"(\d{1,2}(?:\.\d+)?)\s*[,\s]\s*(\d{1,3}(?:\.\d+)?)", text)
    if not found or float(found[1]) > 90 or float(found[2]) > 180:
        raise ValueError("Type the plant's location as latitude, longitude in decimal degrees north and east, "
                         "such as 11.4404, 75.9420.")
    (south, north), (west, east) = INDIA
    if not (south <= float(found[1]) <= north and west <= float(found[2]) <= east):
        raise ValueError("That location isn't in India: type the latitude first, then the longitude, such as "
                         "11.4404, 75.9420.")
    return float(found[1]), float(found[2])


def parse_switch(value, name: str) -> bool:
    """true, "yes", 1 -> True; false, "no", 0 or nothing -> False: a tick box, or a switch in trip.json."""
    text = str(value if value is not None else "").strip().lower()
    if text in ("true", "yes", "1"):
        return True
    if text in ("", "false", "no", "0", "none"):
        return False
    raise ValueError(f"Give {name} as true or false.")
