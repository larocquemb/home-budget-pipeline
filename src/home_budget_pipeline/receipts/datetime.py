#!/usr/bin/env python3
"""Extract transaction date/time from receipt OCR without inventing a timezone."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

MONTH_NAME_RE = re.compile(
    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\s+(\d{1,2}),\s*(20\d{2})(?:\s+(\d{1,2}):(\d{2})(?:\s*([AP]M))?)?\b",
    re.I,
)
YMD_RE = re.compile(r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?\b")
MDY_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d{2}|\d{2})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?\b")
YMD2_RE = re.compile(r"\b(\d{2})/\s*(\d{1,2})\s*[,']?\s*/?\s*[,']?\s*(\d{1,2})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?\b")


def _iso(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0, has_time: bool = False) -> Optional[str]:
    try:
        value = datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None
    return value.isoformat(timespec="seconds") if has_time else value.date().isoformat()


def _hour_12_to_24(hour: int, ampm: Optional[str]) -> int:
    if not ampm:
        return hour
    if hour < 1 or hour > 12:
        return hour
    if ampm.upper() == "AM":
        return 0 if hour == 12 else hour
    return 12 if hour == 12 else hour + 12


def extract_transaction_datetime(text: str) -> Optional[str]:
    """Return ISO local datetime when time exists, otherwise ISO date."""
    for line in text.splitlines():
        m = MONTH_NAME_RE.search(line)
        if m:
            hour = int(m.group(4) or 0)
            has_time = m.group(4) is not None
            hour = _hour_12_to_24(hour, m.group(6))
            value = _iso(int(m.group(3)), MONTHS[m.group(1)[:3].lower()], int(m.group(2)), hour, int(m.group(5) or 0), has_time=has_time)
            if value:
                return value
    for line in text.splitlines():
        m = YMD_RE.search(line)
        if m:
            has_time = m.group(4) is not None
            value = _iso(int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0), has_time)
            if value:
                return value
    for line in text.splitlines():
        m = MDY_RE.search(line)
        if m:
            year = int(m.group(3))
            if year < 100:
                year += 2000
            has_time = m.group(4) is not None
            value = _iso(year, int(m.group(1)), int(m.group(2)), int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0), has_time)
            if value:
                return value
    for line in text.splitlines():
        if not re.search(r"date|time|operator|approved|acct|card|reference|transaction", line, re.I):
            continue
        m = YMD2_RE.search(line)
        if m:
            has_time = m.group(4) is not None
            value = _iso(2000 + int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0), has_time)
            if value:
                return value
    return None


def date_part(value: Optional[str]) -> Optional[str]:
    return value[:10] if value else None
