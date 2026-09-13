"""Record types for scraped Google Maps listings.

The previous implementation stored each field in its own list inside a global
dict (``data.data['names']``, ``data.data['phones']``, ...).  Rows were only
implicitly related by list position, so any field that failed to append -- or
appended twice -- silently shifted every following row into the wrong record.

A listing is now a single object.  Fields are filled in together or not at all,
which makes that class of corruption impossible to express.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Excel headers, in output order.  These match the headers written by earlier
# versions of the project so previously exported workbooks still merge cleanly.
COLUMNS: "dict[str, str]" = {
    "name": "Names",
    "address": "Address",
    "plus_code": "Plus Code",
    "phone": "Phone Number",
    "website": "Website",
    "google_link": "Google Link",
    "latitude": "Latitude",
    "longitude": "Longitude",
    "reviews_count": "Reviews_Count",
    "rating": "Average Rates",
    "category": "Type",
}

HEADERS: "list[str]" = list(COLUMNS.values())

# Matches the "@lat,lon" segment of a Google Maps place URL, e.g.
# https://www.google.com/maps/place/Name/@51.5074,-0.1278,17z/data=...
_COORD_RE = re.compile(r"@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)")

# Digits, separators and any decimal part, e.g. "(1,234)" or "1.2K".
_NUMBER_RE = re.compile(r"\d[\d.,\u00a0\u202f ]*")


def parse_coordinates(url: str) -> "tuple[float | None, float | None]":
    """Return ``(latitude, longitude)`` for a Maps URL, or ``(None, None)``.

    Both values are always produced together, so latitude and longitude can
    never end up at different lengths.  Out-of-range values are rejected rather
    than written to the spreadsheet.
    """
    if not url:
        return None, None
    match = _COORD_RE.search(url)
    if not match:
        return None, None
    try:
        lat = float(match.group(1))
        lon = float(match.group(2))
    except ValueError:
        return None, None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None, None
    return lat, lon


def parse_reviews_count(text: str) -> "int | None":
    """Extract an integer review count from text such as ``"(1,234)"``."""
    if not text:
        return None
    match = _NUMBER_RE.search(text)
    if not match:
        return None
    cleaned = re.sub(r"[^\d]", "", match.group(0))
    if not cleaned:
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def parse_rating(text: str) -> "float | None":
    """Extract a rating from text such as ``"4.5"`` or ``"4,5"`` (comma locale)."""
    if not text:
        return None
    match = re.search(r"\d+(?:[.,]\d+)?", text)
    if not match:
        return None
    try:
        value = float(match.group(0).replace(",", "."))
    except ValueError:
        return None
    # Google ratings are on a 1-5 scale; anything else is a mis-read element.
    if not (0.0 <= value <= 5.0):
        return None
    return value


@dataclass
class Listing:
    """One Google Maps business listing."""

    name: str = ""
    address: str = ""
    plus_code: str = ""
    phone: str = ""
    website: str = ""
    google_link: str = ""
    latitude: "float | None" = None
    longitude: "float | None" = None
    reviews_count: "int | None" = None
    rating: "float | None" = None
    category: str = ""

    def set_coordinates_from_url(self, url: str) -> None:
        """Populate latitude and longitude from a Maps URL as a single unit."""
        self.latitude, self.longitude = parse_coordinates(url)

    @property
    def is_empty(self) -> bool:
        """True when nothing identifying was captured.

        Used to drop placeholder rows instead of writing blank lines to Excel.
        """
        return not (self.name or self.address or self.phone or self.website)

    def to_row(self) -> "dict[str, Any]":
        """Render the listing as an Excel row keyed by output header."""
        return {header: getattr(self, attr) for attr, header in COLUMNS.items()}
