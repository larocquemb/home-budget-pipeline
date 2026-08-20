"""Conservative receipt-total recovery when OCR destroys the printed total.

This is deliberately stricter than the normal total parser. It derives a total
only when one coherent subtotal and one coherent tax amount are present and the
receipt also contains an explicit (though possibly unreadable) total/tender
context. Receipts with multiple subtotal/tax sections remain for review.
"""

from __future__ import annotations

import re
from typing import Optional

MONEY_RE = re.compile(r"-?\$?\s*(\d{1,6}(?:,\d{3})*\.\d{2})-?")
SUBTOTAL_RE = re.compile(r"\bsub\s*tot(?:al|ae|fl)?\b", re.I)
TAX_RE = re.compile(r"\b(?:tax|gst|pst|hst)\b", re.I)
PERCENT_TAX_RE = re.compile(r"\b(?:5|7|8|12|13|14|15)\s*%", re.I)
TOTAL_CONTEXT_RE = re.compile(
    r"\b(?:total|tot\s*al|tot\s*ae|totae|jtal|tender|visa|master\s*card|mastercard|debit|interac)\b",
    re.I,
)


def _money(line: str) -> Optional[float]:
    matches = list(MONEY_RE.finditer(line))
    if not matches:
        return None
    m = matches[-1]
    token = m.group(0).strip()
    value = float(m.group(1).replace(",", ""))
    return -value if token.startswith("-") or token.endswith("-") else value


def reconcile_total_from_text(text: str, parsed_total: Optional[float]) -> Optional[float]:
    """Return parsed_total or a safely derived subtotal+tax total."""
    if parsed_total is not None:
        return parsed_total
    if not TOTAL_CONTEXT_RE.search(text):
        return None

    lines = [re.sub(r"\s+", " ", raw).strip() for raw in text.splitlines()]
    subtotal_rows = []
    for idx, line in enumerate(lines):
        if not SUBTOTAL_RE.search(line):
            continue
        amount = _money(line)
        if amount is not None:
            subtotal_rows.append((idx, amount))

    if len(subtotal_rows) != 1:
        return None

    subtotal_idx, subtotal = subtotal_rows[0]
    tax_candidates = []
    for line in lines[subtotal_idx + 1 : subtotal_idx + 6]:
        amount = _money(line)
        if amount is None:
            continue
        if TAX_RE.search(line) or PERCENT_TAX_RE.search(line):
            tax_candidates.append(amount)

    if len(tax_candidates) != 1:
        return None

    tax = tax_candidates[0]
    if subtotal < 0 or tax < 0:
        return None
    return round(subtotal + tax, 2)
