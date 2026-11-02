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
    r"\s+(\d{1,2})(?:,\s*|\s+)(20\d{2})(?:(?:,\s*|\s+)(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\s*([AP]M))?)?\b",
    re.I,
)
YMD_RE = re.compile(r"(?<!\d)(20\d{2})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})(?:\s+(\d{1,2})\s*:\s*(\d{2})(?:\s*:\s*(\d{2}))?)?(?!\d)")
MDY_RE = re.compile(
    r"(?<![\d.$])(\d{1,2})\s*[-/.]\s*(\d{1,2})\s*"
    r"(?:[-/.]\s*|[,;]\s*['’]?\s*)['’]?(20\d{2}|\d{2})"
    r"(?:\s+(\d{1,2})\s*:\s*(\d{2})(?:\s*:\s*(\d{2}))?(?:\s*([AP]M))?)?(?!\d)", re.I
)
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


# Date purpose is independent of merchant, source filename and document ID.
# Explicit purchase/invoice dates outrank service completion; payment dates are
# a fallback. Print, delivery, policy and other document metadata are excluded.
DATE_PURPOSE_RE = re.compile(r"""
    (?P<transaction>\b(?:(?:transaction|purchase|sale|invoice|order)\s+date(?:/time)?
        |date\s+of\s+(?:purchase|sale|invoice|order)|purchased\s+on)\b)
  | (?P<completion>\b(?:(?:work|service)\s+completed|(?:completion|service)\s+date)\b)
  | (?P<payment>\b(?:payment\s+date|cashier\s+date|date\s+paid|paid\s+on)\b)
  | (?P<metadata>\b(?:(?:date|time)\s*(?:printed|reprinted|generated|downloaded|exported)
        |(?:print(?:ed)?|reprint(?:ed)?|generated|downloaded|exported)(?:\s*(?:date|time|on|at)|(?=\s*[:#0-9])|(?=\s*$))
        |due(?:\s+date)?|delivery(?:\s+date)?|shipped(?:\s+on)?|ship\s+date
        |date\s+promised|promised(?:\s+(?:date|on))?
        |effective|polic(?:y|ies)|expires?|expiry|expiration|clearance|warranty
        |vehicle\s+received)\b)
""", re.I | re.X | re.M)
DAY_MONTH_NAME_RE = re.compile(
    r"\b(\d{1,2})[- ](Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)[- ,]+(20\d{2})"
    r"(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?\b", re.I)
EMBEDDED_YMD_RE = re.compile(r"(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])")


def allows_filename_date(text: str) -> bool:
    """Do not mask missing/conflicting labeled dates with a filename guess."""
    return DATE_PURPOSE_RE.search(text) is None


def _purpose_segments(text):
    pending = None
    ranks = {'transaction': 0, 'completion': 1, 'payment': 2, 'metadata': -1}
    # Browser print headers pair a leading timestamp with a URL/page footer.
    # This is document creation metadata, even when the word "printed" is absent.
    browser_print = bool(re.search(r'https?://[^\n]+(?:\s+\d+\s*/\s*\d+)?', text, re.I))
    print_header = re.compile(
        r"^\s*\d{1,2}/\d{1,2}/(?:\d{4}|\d{2}),\s*\d{1,2}:\d{2}\s*[AP]M(?:\s+.*)?$", re.I,
    )
    for line_number, line in enumerate(text.splitlines()):
        if browser_print and print_header.fullmatch(line):
            continue
        if not line.strip():
            continue
        labels = list(DATE_PURPOSE_RE.finditer(line))
        if not labels:
            yield pending if pending is not None else 3, line_number, line
            pending = None
            continue
        pending = None
        yield 3, line_number, line[:labels[0].start()]
        for i, label in enumerate(labels):
            value = line[label.end():labels[i+1].start() if i+1 < len(labels) else len(line)]
            rank = ranks[label.lastgroup]
            yield rank, line_number, value
            if i == len(labels)-1 and not value.strip(' :#=-\t'):
                pending = rank


def _date_candidates(text, *, labeled=False):
    full_date_spans = []
    for m in MONTH_NAME_RE.finditer(text):
        hour = _hour_12_to_24(int(m.group(4) or 0), m.group(7))
        value = _iso(int(m.group(3)), MONTHS[m.group(1)[:3].lower()], int(m.group(2)),
                     hour, int(m.group(5) or 0), int(m.group(6) or 0), has_time=m.group(4) is not None)
        if value:
            yield 0, m.start(), value
    for m in YMD_RE.finditer(text):
        value = _iso(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                     int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0), m.group(4) is not None)
        if value:
            full_date_spans.append(m.span())
            yield 1, m.start(), value
    for m in MDY_RE.finditer(text):
        first = text[m.end(1):m.start(2)].strip()
        second = text[m.end(2):m.start(3)].strip().strip("'’").strip()
        if second in {'-', '/', '.'} and first != second:
            continue
        year = int(m.group(3))
        value = _iso(year + 2000 if year < 100 else year, int(m.group(1)), int(m.group(2)),
                     _hour_12_to_24(int(m.group(4) or 0), m.group(7)), int(m.group(5) or 0), int(m.group(6) or 0), m.group(4) is not None)
        if value:
            full_date_spans.append(m.span())
            yield 2, m.start(), value
    if labeled or re.search(r'date|time|operator|approved|acct|card|reference|transaction', text, re.I):
        for m in YMD2_RE.finditer(text):
            if any(start < m.end() and m.start() < end for start, end in full_date_spans):
                continue
            value = _iso(2000 + int(m.group(1)), int(m.group(2)), int(m.group(3)),
                         int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0), m.group(4) is not None)
            if value:
                yield 3, m.start(), value
    for m in DAY_MONTH_NAME_RE.finditer(text):
        value = _iso(int(m.group(3)), MONTHS[m.group(2)[:3].lower()], int(m.group(1)),
                     int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0), m.group(4) is not None)
        if value:
            yield 4, m.start(), value
    for digits in re.finditer(r'\d{14,}', text):
        for m in EMBEDDED_YMD_RE.finditer(digits.group()):
            value = _iso(*(int(m.group(i)) for i in (1, 2, 3)))
            if value:
                yield 5, digits.start()+m.start(), value


def _with_labeled_time(value: str, line_number: int, lines: list[str]) -> str:
    """Join a date to an explicit time on its own or an adjacent time-only row."""
    if 'T' in value:
        return value
    time_re = re.compile(r'\btime\s*[:;]?\s*(\d{1,2}):\s*(\d{2})(?::\s*(\d{2}))?\s*([AP]M)?\b', re.I)
    # Terminal OCR can place response/auth rows between DATE and TIME. Cross
    # only recognized terminal rows, with a four-row bound in each direction.
    indexes = [line_number]
    for direction in (1, -1):
        for distance in range(1, 5):
            index = line_number + direction * distance
            if not 0 <= index < len(lines):
                break
            row = lines[index].strip()
            if time_re.fullmatch(row):
                indexes.append(index)
                break
            if not re.fullmatch(r'(?:RESP(?:ONSE)?\b.*|AUTH\b.*|APPR(?:OVAL)?\b.*|RF|\d{1,6})?', row, re.I):
                break
    for index in indexes:
        if not 0 <= index < len(lines):
            continue
        line = lines[index].strip()
        if any(m.lastgroup == 'metadata' for m in DATE_PURPOSE_RE.finditer(line)):
            continue
        match = time_re.search(line) if index == line_number else time_re.fullmatch(line)
        if match:
            timestamp = _iso(int(value[:4]), int(value[5:7]), int(value[8:10]),
                             _hour_12_to_24(int(match[1]), match[4]), int(match[2]), int(match[3] or 0), True)
            if timestamp:
                return timestamp
    return value


def extract_transaction_datetime(text: str) -> Optional[str]:
    """Choose dates by their purpose, never by print order or a merchant rule.

    Conflicting dates at the strongest explicit purpose remain unknown. The
    original text is untouched, retaining print/receipt/completion provenance.
    """
    candidates = transaction_datetime_evidence(text)
    if not candidates:
        return None
    rank = min(c['purpose_rank'] for c in candidates)
    choices = [c for c in candidates if c['purpose_rank'] == rank]
    if rank < 3 and len({c['value'][:10] for c in choices}) > 1:
        return None
    return min(choices, key=lambda c: (c['format_rank'], c['line_number'], c['position']))['value']


def transaction_datetime_evidence(text: str) -> list[dict]:
    """Expose purpose-ranked original readings for cross-pass field decisions."""
    candidates = []
    lines = text.splitlines()
    for rank, line_number, segment in _purpose_segments(text):
        if rank < 0:
            continue
        for fmt, position, value in _date_candidates(segment, labeled=rank < 3):
            patterns = {0: (MONTH_NAME_RE, 3), 1: (YMD_RE, 1), 2: (MDY_RE, 3),
                        3: (YMD2_RE, 1), 4: (DAY_MONTH_NAME_RE, 3)}
            pattern, year_group = patterns.get(fmt, (None, None))
            match = pattern.match(segment, position) if pattern else None
            candidates.append(dict(purpose_rank=rank, format_rank=fmt, line_number=line_number + 1,
                position=position, value=_with_labeled_time(value, line_number, lines),
                year_digits=len(match[year_group]) if match else None))
    return candidates


def date_part(value: Optional[str]) -> Optional[str]:
    return value[:10] if value else None
