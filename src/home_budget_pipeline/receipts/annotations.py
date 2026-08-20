"""Extract useful semantic annotations from scanned receipt OCR.

Paper receipts can contain checkout-time category boundaries and handwritten
notes that do not exist in the electronic receipt.  This module deliberately
captures evidence without guessing a normalized budget category.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import Optional

MONEY_RE = re.compile(r"-?\$?\s*(\d{1,6}(?:,\d{3})*\.\d{2})-?")
SUBTOTAL_RE = re.compile(r"\bsub\s*tot(?:al|ae|fl)?\b", re.I)
NOTE_RE = re.compile(r"\b(?:owes?|note|paul|rox(?:anne)?|chloe|myl[eè]ne|daniel|julianne)\b", re.I)


@dataclass(frozen=True)
class ReceiptAnnotation:
    annotation_type: str
    text: str
    amount: Optional[float] = None
    line_index: Optional[int] = None
    normalized_category: Optional[str] = None
    confidence: float = 1.0

    def to_dict(self) -> dict:
        return asdict(self)


def _money(line: str) -> Optional[float]:
    matches = list(MONEY_RE.finditer(line))
    if not matches:
        return None
    m = matches[-1]
    token = m.group(0).strip()
    value = float(m.group(1).replace(",", ""))
    return -value if token.startswith("-") or token.endswith("-") else value


def extract_receipt_annotations(text: str) -> list[ReceiptAnnotation]:
    """Extract non-destructive category-subtotal and note evidence.

    A repeated subtotal is treated as a checkout/category boundary clue, not as
    the canonical receipt total.  Category names are intentionally left unset
    unless they are explicitly known from stronger evidence later.
    """
    lines = [re.sub(r"\s+", " ", raw).strip() for raw in text.splitlines()]
    subtotal_rows: list[tuple[int, str, float]] = []
    notes: list[ReceiptAnnotation] = []

    for idx, line in enumerate(lines):
        if not line:
            continue
        if SUBTOTAL_RE.search(line):
            amount = _money(line)
            if amount is not None:
                subtotal_rows.append((idx, line, amount))
        elif NOTE_RE.search(line):
            notes.append(
                ReceiptAnnotation(
                    annotation_type="note",
                    text=line,
                    line_index=idx,
                    confidence=0.65,
                )
            )

    annotations: list[ReceiptAnnotation] = []
    if len(subtotal_rows) > 1:
        for idx, line, amount in subtotal_rows:
            annotations.append(
                ReceiptAnnotation(
                    annotation_type="category_subtotal",
                    text=line,
                    amount=amount,
                    line_index=idx,
                    confidence=0.95,
                )
            )

    annotations.extend(notes)
    return annotations
