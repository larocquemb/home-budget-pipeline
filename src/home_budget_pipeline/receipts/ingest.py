#!/usr/bin/env python3
"""Discover scanned paper receipts, OCR them, and optionally upsert canonical records.

KAN-76 design goals:
- preserve the original scan as evidence (filename/reference + SHA-256)
- support PDF/image scans and multi-page receipts
- remove repeated OCR lines at adjacent page boundaries
- extract common receipt fields without assuming one merchant
- write budget.expenses + budget.expense_items idempotently
- assign extraction confidence/status so weak scans can be reviewed
- retain payment method/card provenance and resolve payer from time-bounded DB data

OCR is deliberately local. Image-based PDFs are re-OCRed after receipt-focused
preprocessing instead of blindly trusting a potentially inaccurate embedded text layer.
The raw OCR text and page text are retained in raw_payload for later reprocessing.
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from difflib import SequenceMatcher
import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence, Tuple
from uuid import uuid4

from .. import telemetry
from payment_card_lookup import resolve_owner_from_db
from receipt_payment import extract_payment_provenance

SUPPORTED_EXTS = {".pdf", ".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff"}
OCR_RENDER_DPIS = (150, 200, 300)
OCR_PAGE_SEGMENTATION_MODES = ("4", "6", "11")
OCR_HIGH_DETAIL_DPI = 450
OCR_HIGH_DETAIL_PSM = "6"
OCR_SELECTION_POLICY = "observed-header-and-items-v1"
MONEY_RE = re.compile(r"-?\$?\s*(\d{1,6}(?:,\d{3})*\.\d{2})-?")
OCR_MONEY_RE = re.compile(
    r"(?P<lead>-)?\$?\s*(?P<whole>\d{1,6})(?:\s*[,.:]\s*\.?\s*|\s+\.\s*)(?P<cents>\d{2})(?P<trail>-)?"
)
TESSERACT_TSV_ROW_RE = re.compile(
    r"(?:^|\s)[1-5](?:\s+\d+){9}\s+-?\d+(?:\.\d+)?(?:\s|$)"
)
ID_PATTERNS = (
    re.compile(r"\b(?:receipt|transaction|trans|order|invoice)\b\s*(?:#|no\.?|number|id)?\s*[:#-]?\s*([A-Z0-9-]{4,})\b", re.I),
)
TOTAL_WORDS = re.compile(r"\b(total|subtotal|tax|gst|pst|hst|balance|tender|change|amount\s+due)\b", re.I)
NON_ITEM_WORDS = re.compile(
    r"\b(thank|visa|mastercard|debit|credit|approved|cashier|store|points?|savings?|saved|discount|coupon|EHC|deposit)\b",
    re.I,
)
TENDER_LINE_RE = re.compile(
    r"(?:\bacct\s*:.*cad\$?|\bcad\$|\bvisa(?:\s+credit(?:\s+card)?)?|\bmastercard|\bmaster\s*card|"
    r"\bdebit|\binterac|\bamex|\btender|\btrans\s+type\s*:\s*purchase|^\s*(?:a?mount|mount)\b)",
    re.I,
)
TOTAL_LIKE_RE = re.compile(
    r"\b(?:grand\s+total|purchase\s+total|amount\s+due|balance\s+due|total\s+due|total|tot(?:\s*al|ae|fl)|jtal)\b",
    re.I,
)

MERCHANT_ALIASES = (
    (re.compile(r"\bclearview\s+co[ -]?op\b", re.I), "Clearview Co-op"),
    (re.compile(r"\b(?:m|rn)aurices\b", re.I), "maurices"),
    (re.compile(r"canad(?:ian|\s*tan|\w*)?\s+tire", re.I), "Canadian Tire"),
    (re.compile(r"(?:t|i|ti|im|tin|tim)\w*\s+hortons", re.I), "Tim Hortons"),
    (re.compile(r"\bwholesale\s+club\b", re.I), "Wholesale Club"),
    (re.compile(r"shoppers", re.I), "Shoppers Drug Mart"),
    (re.compile(r"best\s+buy", re.I), "Best Buy"),
    (re.compile(r"walmart", re.I), "Walmart"),
    (re.compile(r"sobeys", re.I), None),
)


@dataclass
class ScannedItem:
    item_name: str
    line_total: float
    department: Optional[str] = None


@dataclass
class ScannedReceipt:
    path: str
    source_reference: str
    source_sha256: str
    merchant: Optional[str]
    transaction_date: Optional[str]
    receipt_id: Optional[str]
    subtotal: Optional[float]
    tax: Optional[float]
    total: Optional[float]
    payment_method: Optional[str] = None
    card_last4: Optional[str] = None
    payer: Optional[str] = None
    items: List[ScannedItem] = field(default_factory=list)
    page_text: List[str] = field(default_factory=list)
    ocr_layout: List[dict] = field(default_factory=list)
    text: str = ""
    extraction_confidence: float = 0.0
    extraction_status: str = "review"
    review_reasons: List[str] = field(default_factory=list)
    store: Optional[str] = None
    field_decisions: dict = field(default_factory=dict)
    store_address: Optional[str] = None
    store_phone: Optional[str] = None
    tax_registration_number: Optional[str] = None

    @property
    def canonical_order_id(self) -> str:
        return f"scan:{self.source_sha256}"


@dataclass(frozen=True)
class OCRLine:
    text: str
    confidence: float
    x: Optional[int] = None
    y: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None


@dataclass(frozen=True)
class OCRCandidate:
    text: str
    lines: Tuple[OCRLine, ...]
    dpi: int
    psm: str
    engine: str = "tesseract"
    status: str = "success"
    error_type: Optional[str] = None


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Ingest scanned paper receipts into the canonical budget pipeline.")
    p.add_argument("input_path", help="Scan file or directory containing scanned receipts.")
    p.add_argument("--write-db", action="store_true", help="Upsert budget.expenses and budget.expense_items.")
    p.add_argument("--db-dsn", default=os.getenv("HOME_BUDGET_PG_DSN", ""))
    p.add_argument("--db-schema", default="budget")
    p.add_argument("--json", dest="json_path", default="", help="Optional JSON report path (keep local; may contain financial data).")
    p.add_argument("--show-review", action="store_true", help="Print one compact line for each receipt requiring review or unreadable.")
    return p.parse_args()


def discover_scans(root: Path) -> List[Path]:
    if root.is_file():
        return [root] if root.suffix.lower() in SUPPORTED_EXTS else []
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _tesseract_environment() -> dict[str, str] | None:
    threads = os.getenv("HOME_BUDGET_OCR_THREADS")
    if not threads:
        return None
    count = int(threads)
    if count < 1:
        raise ValueError("HOME_BUDGET_OCR_THREADS must be positive")
    return {**os.environ, "OMP_NUM_THREADS": str(count), "OMP_THREAD_LIMIT": str(count)}


def _run_tesseract(path: Path, psm: str = "4") -> str:
    binary = shutil.which("tesseract")
    if not binary:
        return ""
    proc = subprocess.run([binary, str(path), "stdout", "--psm", psm], capture_output=True, text=True, check=False, env=_tesseract_environment())
    return proc.stdout if proc.returncode == 0 else ""


def _run_tesseract_candidate(path: Path, *, dpi: int, psm: str) -> OCRCandidate:
    """Run Tesseract once and retain reconstructed lines and word confidence."""
    binary = shutil.which("tesseract")
    if not binary:
        return OCRCandidate("", (), dpi, psm, status="unavailable", error_type="executable_not_found")
    proc = subprocess.run(
        [binary, str(path), "stdout", "--psm", psm, "tsv"],
        capture_output=True,
        text=True,
        check=False,
        env=_tesseract_environment(),
    )
    if proc.returncode != 0:
        return OCRCandidate("", (), dpi, psm, status="failed", error_type="nonzero_exit")
    grouped: dict[Tuple[str, str, str, str], list[Tuple[str, float, int, int, int, int]]] = {}
    for row in csv.DictReader(io.StringIO(proc.stdout), delimiter="\t"):
        word = (row.get("text") or "").strip()
        if row.get("level") != "5" or not word:
            continue
        key = tuple(row.get(field, "") for field in ("page_num", "block_num", "par_num", "line_num"))
        try:
            confidence = max(0.0, float(row.get("conf", "-1")))
        except ValueError:
            confidence = 0.0
        try:
            box = tuple(int(row.get(field, "0")) for field in ("left", "top", "width", "height"))
        except ValueError:
            box = (0, 0, 0, 0)
        grouped.setdefault(key, []).append((word, confidence, *box))
    lines: list[OCRLine] = []
    for words in grouped.values():
        text = normalize_line(" ".join(word for word, *_ in words))
        weight = sum(len(word) for word, *_ in words)
        confidence = sum(len(word) * value for word, value, *_ in words) / weight if weight else 0.0
        x = min(word[2] for word in words)
        y = min(word[3] for word in words)
        right = max(word[2] + word[4] for word in words)
        bottom = max(word[3] + word[5] for word in words)
        lines.append(OCRLine(text, confidence, x, y, right - x, bottom - y))
    return OCRCandidate("\n".join(line.text for line in lines), tuple(lines), dpi, psm)


_PADDLE_OCR = None


def _paddle_ocr_enabled() -> bool:
    return os.getenv("HOME_BUDGET_PADDLE_OCR", "").strip().lower() in {"1", "true", "yes", "on"}


def paddle_mkldnn_enabled() -> bool:
    """Enable oneDNN on the Linux/x86 runtime pinned for production CPU OCR."""
    return platform.system() == "Linux" and platform.machine().lower() == "x86_64"


def _run_paddle_candidate(image, *, dpi: int) -> OCRCandidate:
    """Run the optional Paddle engine and retain its line confidence."""
    global _PADDLE_OCR
    if not _paddle_ocr_enabled():
        return OCRCandidate("", (), dpi, "paddle", "paddle", "disabled")
    try:
        if _PADDLE_OCR is None:
            from paddleocr import PaddleOCR  # type: ignore

            thread_options = {}
            if os.getenv("HOME_BUDGET_OCR_THREADS"):
                threads = int(os.environ["HOME_BUDGET_OCR_THREADS"])
                if threads < 1:
                    raise ValueError("HOME_BUDGET_OCR_THREADS must be positive")
                thread_options["cpu_threads"] = threads
            _PADDLE_OCR = PaddleOCR(
                text_detection_model_name=os.getenv("HOME_BUDGET_PADDLE_DET_MODEL", "PP-OCRv6_medium_det"),
                text_recognition_model_name=os.getenv("HOME_BUDGET_PADDLE_REC_MODEL", "PP-OCRv6_medium_rec"),
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                # Linux/x86 pins Paddle 3.2.2: 3.3.1 regresses the oneDNN
                # PIR converter used by PP-OCRv6. Keep acceleration enabled.
                enable_mkldnn=paddle_mkldnn_enabled(),
                **thread_options,
            )
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            image.save(tmp_path)
            result = list(_PADDLE_OCR.predict(str(tmp_path)))[0].json["res"]
        finally:
            tmp_path.unlink(missing_ok=True)
        lines = tuple(
            OCRLine(normalize_line(str(text)), float(score) * 100.0)
            for text, score in zip(result.get("rec_texts", ()), result.get("rec_scores", ()))
            if normalize_line(str(text))
        )
        return OCRCandidate("\n".join(line.text for line in lines), lines, dpi, "paddle", "paddle")
    except Exception as exc:
        return OCRCandidate("", (), dpi, "paddle", "paddle", "failed", type(exc).__name__)


def _prepare_ocr_image(image):
    """Crop scanner whitespace and enhance faint thermal-receipt printing."""
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps

    gray = image.convert("L")
    width, height = gray.size
    # Requiring several dark pixels per row/column ignores isolated scanner
    # marks while retaining the receipt edges and printed content.
    min_column_ink = max(12, height // 200)
    min_row_ink = max(20, width // 100)
    ink = gray.point(lambda value: 255 if value < 225 else 0)
    # BOX resampling computes the projections in native code. Converting every
    # pixel in Python made a single full-page scan take minutes.
    column_projection = ink.resize((width, 1), resample=Image.Resampling.BOX)
    row_projection = ink.resize((1, height), resample=Image.Resampling.BOX)
    columns = list(column_projection.get_flattened_data())
    rows = list(row_projection.get_flattened_data())
    column_threshold = 255 * min_column_ink / height
    row_threshold = 255 * min_row_ink / width
    xs = [x for x, value in enumerate(columns) if value >= column_threshold]
    ys = [y for y, value in enumerate(rows) if value >= row_threshold]
    if xs and ys:
        margin = max(12, min(width, height) // 100)
        gray = gray.crop((
            max(0, min(xs) - margin),
            max(0, min(ys) - margin),
            min(width, max(xs) + margin + 1),
            min(height, max(ys) + margin + 1),
        ))
    gray = ImageOps.autocontrast(gray, cutoff=1)
    gray = ImageEnhance.Contrast(gray).enhance(3.0)
    if gray.width < 1800:
        gray = gray.resize((gray.width * 2, gray.height * 2))
    return gray.filter(ImageFilter.UnsharpMask(radius=1, percent=180, threshold=2))


def _has_literal_valid_timestamp(text: str) -> bool:
    """Recognize a valid date/time in the same bounded context as the parser."""
    from .datetime import transaction_datetime_evidence
    return any('T' in reading['value'] for reading in transaction_datetime_evidence(text))


def _looks_like_tesseract_tsv(text: str) -> bool:
    """Detect a flattened Tesseract TSV layer masquerading as receipt text."""
    if re.search(r'\blevel\s+page_num\s+block_num\s+par_num\s+line_num\s+word_num\b', text):
        return True
    return len(TESSERACT_TSV_ROW_RE.findall(text)) >= 3


def _usable_embedded_text(text: str) -> str:
    """Reject PDF text layers containing OCR engine metadata rather than prose."""
    return "" if _looks_like_tesseract_tsv(text) else text


def _ocr_candidate_score(text: str) -> int:
    """Score OCR using receipt structure without merchant-specific assumptions."""
    score = min(200, len(re.sub(r"\W", "", text)) // 5)
    if _has_literal_valid_timestamp(text):
        score += 500
    score += min(5, len(MONEY_RE.findall(text))) * 20
    score += min(3, len(re.findall(r"\b(?:sub\s*total|total|tax|gst|pst|hst)\b", text, re.I))) * 40
    score += min(2, len(re.findall(r"\b(?:receipt|transaction|invoice|order|auth)\b", text, re.I))) * 25
    return score


def _ocr_supplement_score(text: str) -> int:
    subtotal, tax, total = extract_totals(text)
    payment = extract_payment_provenance(text)
    return (
        (40 if subtotal is not None else 0)
        + (60 if tax is not None else 0)
        + (100 if total is not None else 0)
        + (40 if payment.payment_method else 0)
        + (100 if payment.card_last4 else 0)
    )


def _select_ocr_candidate(candidates: Sequence[OCRCandidate]) -> OCRCandidate:
    """Prefer coherent receipt fields over text volume and OCR noise."""
    candidates = [c for c in candidates if c.text and not _looks_like_tesseract_tsv(c.text)]
    merchants = [extract_merchant(candidate.text) for candidate in candidates]
    totals = [extract_totals(candidate.text, infer_tax=False)[2] for candidate in candidates]

    def rank(candidate):
        subtotal, tax, total = extract_totals(candidate.text, infer_tax=False)
        header_ok = (subtotal is not None and tax is not None and total is not None
                     and abs(round(subtotal + tax - total, 2)) <= .01)
        items = extract_items(candidate.text)
        items_ok = (bool(items) and subtotal is not None
                    and abs(round(sum(item.line_total for item in items) - subtotal, 2)) <= .01)
        merchant = extract_merchant(candidate.text)
        key = _line_key(merchant or '')
        support = sum(1 for other in merchants if key and other
                      and SequenceMatcher(None, key, _line_key(other)).ratio() >= .85)
        confidence = (sum(line.confidence for line in candidate.lines) / len(candidate.lines)
                      if candidate.lines else 0)
        total_support = sum(1 for other in totals if total is not None and other is not None
                            and abs(round(total - other, 2)) <= .01)
        direction_ok = True
        if subtotal is not None and total is not None:
            direction_ok = subtotal * total >= 0
            expected_tax = round(total - subtotal, 2)
            if tax is not None:
                direction_ok = direction_ok and (abs(tax) <= .01 if abs(expected_tax) <= .01
                                                  else tax * expected_tax >= 0)
        return (header_ok, direction_ok, total_support if not header_ok else 0,
                items_ok, total_support if header_ok else 0,
                support if header_ok else 0, confidence if header_ok else 0,
                _ocr_candidate_score(candidate.text), candidate.dpi, candidate.psm)

    return max(
        candidates,
        key=rank,
        default=OCRCandidate("", (), 0, ""),
    )


def _ocr_lines_match(left: str, right: str) -> bool:
    left_key, right_key = _line_key(left), _line_key(right)
    if not left_key or not right_key:
        return False
    left_amount, right_amount = parse_money(left), parse_money(right)
    if left_amount is not None and right_amount is not None and abs(left_amount - right_amount) > 0.001:
        return False
    left_description = _line_key(re.sub(r"\b(?:BC|NC|TP|C)\b\s*$", "", MONEY_RE.sub("", left)))
    right_description = _line_key(re.sub(r"\b(?:BC|NC|TP|C)\b\s*$", "", MONEY_RE.sub("", right)))
    return max(
        SequenceMatcher(None, left_key, right_key).ratio(),
        SequenceMatcher(None, left_description, right_description).ratio(),
    ) >= 0.78


def _merge_ocr_line_evidence(base: str, winner: str) -> str:
    """Keep a winning description while retaining base amount/tax columns."""
    if parse_money(winner) is None:
        money = MONEY_RE.search(base)
        if money:
            return normalize_line(f"{winner} {base[money.start():]}")
    return winner


def _single_glyph_item_variant(left: str, right: str) -> bool:
    """Match same-price item descriptions that differ by one OCR glyph."""
    left_amount, right_amount = parse_money(left), parse_money(right)
    if left_amount is None or right_amount is None or abs(left_amount - right_amount) > 0.001:
        return False
    left_key = _line_key(re.sub(r"\b(?:BC|NC|TP|C)\b\s*$", "", MONEY_RE.sub("", left))).replace(" ", "")
    right_key = _line_key(re.sub(r"\b(?:BC|NC|TP|C)\b\s*$", "", MONEY_RE.sub("", right))).replace(" ", "")
    return (
        len(left_key) >= 6
        and len(left_key) == len(right_key)
        and sum(a != b for a, b in zip(left_key, right_key)) == 1
    )


def _reconcile_duplicate_ocr_lines(lines: list[Tuple[str, float, str]]) -> list[str]:
    """Prefer Paddle spelling, then confidence, for one-glyph duplicates."""
    reconciled = [text for text, _, _ in lines]
    for index, (text, confidence, engine) in enumerate(lines):
        variants = [
            (other_text, other_confidence, other_engine)
            for other_text, other_confidence, other_engine in lines
            if _single_glyph_item_variant(text, other_text)
        ]
        if variants:
            winner, _, _ = max(
                [(text, confidence, engine), *variants],
                key=lambda item: (item[2] == "paddle", item[1]),
            )
            reconciled[index] = _merge_ocr_line_evidence(text, winner)
    return reconciled


def _line_consensus_text(candidates: Sequence[OCRCandidate]) -> str:
    """Choose each base line using aligned OCR confidence and resolution evidence."""
    candidates = [c for c in candidates if c.text and not _looks_like_tesseract_tsv(c.text)]
    base = _select_ocr_candidate(candidates)
    if not base.lines:
        return base.text
    chosen: list[Tuple[str, float, str]] = []
    for base_index, base_line in enumerate(base.lines):
        occurrence = sum(
            1 for prior in base.lines[:base_index] if _ocr_lines_match(base_line.text, prior.text)
        )
        alternatives: list[Tuple[OCRLine, OCRCandidate]] = []
        for candidate in candidates:
            matches = [line for line in candidate.lines if _ocr_lines_match(base_line.text, line.text)]
            if matches:
                alternatives.append((matches[min(occurrence, len(matches) - 1)], candidate))
        if not alternatives:
            chosen.append((base_line.text, base_line.confidence, base.engine))
            continue
        support = Counter(_line_key(line.text) for line, _ in alternatives)
        def evidence_score(alternative: Tuple[OCRLine, OCRCandidate]) -> float:
            return (
                alternative[0].confidence
                + min(18.0, alternative[1].dpi / 25.0)
                + min(2.0, support[_line_key(alternative[0].text)] * 0.5)
                + (8.0 if alternative[1].engine == "paddle" else 0.0)
            )

        winner, winner_candidate = max(
            alternatives,
            key=lambda alternative: (
                1 if _has_literal_valid_timestamp(alternative[0].text) else 0,
                1 if alternative[1].engine == "paddle" else 0,
                evidence_score(alternative),
                alternative[1].dpi,
            ),
        )
        chosen.append(
            (
                _merge_ocr_line_evidence(base_line.text, winner.text),
                evidence_score((winner, winner_candidate)),
                winner_candidate.engine,
            )
        )
    result = "\n".join(_reconcile_duplicate_ocr_lines(chosen))
    original = extract_totals(base.text, infer_tax=False)
    if (all(value is not None for value in original)
            and abs(round(original[0] + original[1] - original[2], 2)) <= .01
            and extract_totals(result, infer_tax=False) != original):
        # Unaligned numeric lines must not change a coherent base header.
        return base.text
    return result


def select_ocr_page(candidates: Sequence[OCRCandidate]) -> str:
    """Select and supplement observations without receipt-specific answers."""
    candidates = [c for c in candidates if c.text and not _looks_like_tesseract_tsv(c.text)]
    text = _line_consensus_text(candidates)
    for candidate in sorted(candidates, key=lambda item: _ocr_supplement_score(item.text), reverse=True):
        text = _supplement_missing_receipt_summary(text, candidate.text)
    return text


def _ocr_pass_metric(candidate: OCRCandidate, *, page: int, variant: str, seconds: float) -> dict:
    structural_score = _ocr_candidate_score(candidate.text)
    summary_score = _ocr_supplement_score(candidate.text)
    valid_timestamp = _has_literal_valid_timestamp(candidate.text)
    line_count = len(candidate.lines)
    character_count = len(candidate.text)
    engine_options = (
        {
            "detection_model": os.getenv("HOME_BUDGET_PADDLE_DET_MODEL", "PP-OCRv6_medium_det"),
            "recognition_model": os.getenv("HOME_BUDGET_PADDLE_REC_MODEL", "PP-OCRv6_medium_rec"),
            "enable_mkldnn": paddle_mkldnn_enabled(),
        }
        if candidate.engine == "paddle"
        else {"psm": candidate.psm}
    )
    engine_type = "vision_ai" if candidate.engine in {"openai", "anthropic", "gemini"} else "traditional_ocr"
    status = candidate.status if candidate.status != "success" or candidate.lines else "no_text"
    return {
        "schema_version": 1,
        "page_number": page,
        "engine": candidate.engine,
        "engine_type": engine_type,
        "dpi": candidate.dpi,
        "psm": candidate.psm,
        "variant": variant,
        "seconds": round(seconds, 3),
        "status": status,
        "error_type": candidate.error_type,
        "line_count": line_count,
        "character_count": character_count,
        "text": candidate.text,
        "structural_score": structural_score,
        "summary_score": summary_score,
        "valid_timestamp": valid_timestamp,
        "selected_base": False,
        "consensus_line_coverage": 0,
        "consensus_coverage_ratio": 0.0,
        "quality": {
            "selection_policy": OCR_SELECTION_POLICY,
            "mean_confidence": (round(sum(line.confidence for line in candidate.lines) / len(candidate.lines), 4)
                                if candidate.lines else None),
            "line_count": line_count,
            "character_count": character_count,
            "structural_score": structural_score,
            "summary_score": summary_score,
            "valid_timestamp": valid_timestamp,
            "consensus_line_coverage": 0,
            "consensus_coverage_ratio": 0.0,
        },
        "engine_options": engine_options,
        "usage": {},
        "provenance": {"lines": [
            {"line_number": index + 1, "text": line.text,
             "confidence": line.confidence,
             "x": line.x, "y": line.y, "width": line.width, "height": line.height}
            for index, line in enumerate(candidate.lines)
        ]},
    }


def _run_traced_ocr_pass(
    operation: Callable[[], OCRCandidate],
    pass_metrics: List[dict] | None,
    *,
    run_uuid: str,
    page: int,
    engine: str,
    dpi: int,
    psm: str,
    variant: str,
) -> tuple[OCRCandidate, tuple[Any, dict, int] | None]:
    """Run one engine pass and keep its span open for consensus attributes."""
    if pass_metrics is None:
        return operation(), None
    pass_id = len(pass_metrics) + 1
    pass_span = telemetry.start_ocr_pass({
        "run_uuid": run_uuid,
        "pass_id": pass_id,
        "receipt.page.number": page,
        "ocr.engine": engine,
        "ocr.dpi": dpi,
        "ocr.psm": psm,
        "ocr.variant": variant,
    })
    started = time.perf_counter()
    try:
        candidate = operation()
    except BaseException as exc:
        metric = {
            **_ocr_pass_metric(
                OCRCandidate("", (), dpi, psm, engine, "failed", type(exc).__name__),
                page=page,
                variant=variant,
                seconds=time.perf_counter() - started,
            ),
            "run_uuid": run_uuid,
            "pass_id": pass_id,
        }
        pass_metrics.append(metric)
        telemetry.record_ocr_pass_completion(pass_span, metric)
        telemetry.finish_ocr_pass(pass_span, metric)
        raise
    completed_at = time.time_ns()
    metric = {
        **_ocr_pass_metric(
            candidate,
            page=page,
            variant=variant,
            seconds=time.perf_counter() - started,
        ),
        "run_uuid": run_uuid,
        "pass_id": pass_id,
    }
    pass_metrics.append(metric)
    telemetry.record_ocr_pass_completion(pass_span, metric)
    return candidate, (pass_span, metric, completed_at)


def _ocr_rendered_pages(
    render_page: Callable[[int, int], Any],
    page_count: int,
    pass_metrics: Optional[List[dict]] = None,
    *,
    run_uuid: str | None = None,
) -> List[str]:
    """Apply the same OCR observations, selection and tracing to every page format."""
    pages: List[str] = []
    selected_run_uuid = run_uuid or telemetry.current_run_uuid() or str(uuid4())
    unfinished_passes: list[tuple[Any, dict, int]] = []
    page_span = None
    try:
        for idx in range(page_count):
            page_span = telemetry.start_operation(
                "receipt.page.processing", {"receipt.page.number": idx + 1},
            )
            candidates: List[OCRCandidate] = []
            # Resolution changes the apparent shape of thermal-printer glyphs,
            # so evaluate every DPI/layout combination instead of assuming the
            # highest resolution or first valid timestamp is most accurate.
            for dpi in OCR_RENDER_DPIS:
                raw_image = render_page(idx, dpi)
                # Raw rendering preserves separation in heavy/bold glyphs;
                # enhancement recovers faint thermal printing.
                for variant, image in (("raw", raw_image), ("enhanced", _prepare_ocr_image(raw_image))):
                    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                        tmp_path = Path(tmp.name)
                    try:
                        image.save(tmp_path)
                        for psm in OCR_PAGE_SEGMENTATION_MODES:
                            candidate, tracked = _run_traced_ocr_pass(
                                lambda: _run_tesseract_candidate(tmp_path, dpi=dpi, psm=psm),
                                pass_metrics,
                                run_uuid=selected_run_uuid,
                                page=idx + 1,
                                engine="tesseract",
                                dpi=dpi,
                                psm=psm,
                                variant=variant,
                            )
                            candidates.append(candidate)
                            if tracked is not None:
                                unfinished_passes.append(tracked)
                    finally:
                        tmp_path.unlink(missing_ok=True)
                if dpi == 200 and _paddle_ocr_enabled():
                    candidate, tracked = _run_traced_ocr_pass(
                        lambda: _run_paddle_candidate(raw_image, dpi=dpi),
                        pass_metrics,
                        run_uuid=selected_run_uuid,
                        page=idx + 1,
                        engine="paddle",
                        dpi=dpi,
                        psm="paddle",
                        variant="raw",
                    )
                    candidates.append(candidate)
                    if tracked is not None:
                        unfinished_passes.append(tracked)
            # A single high-resolution structured pass recovers small final
            # glyphs (for example, a trailing "c") without multiplying every
            # expensive DPI/layout/preprocessing combination.
            high_detail_image = render_page(idx, OCR_HIGH_DETAIL_DPI)
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp_path = Path(tmp.name)
            try:
                high_detail_image.save(tmp_path)
                candidate, tracked = _run_traced_ocr_pass(
                    lambda: _run_tesseract_candidate(
                        tmp_path, dpi=OCR_HIGH_DETAIL_DPI, psm=OCR_HIGH_DETAIL_PSM,
                    ),
                    pass_metrics,
                    run_uuid=selected_run_uuid,
                    page=idx + 1,
                    engine="tesseract",
                    dpi=OCR_HIGH_DETAIL_DPI,
                    psm=OCR_HIGH_DETAIL_PSM,
                    variant="high_detail",
                )
                candidates.append(candidate)
                if tracked is not None:
                    unfinished_passes.append(tracked)
            finally:
                tmp_path.unlink(missing_ok=True)
            page_tracked = unfinished_passes[-len(candidates):] if pass_metrics is not None else []
            with telemetry.span("receipt.consensus.selection", {"receipt.page.number": idx + 1}) as consensus_span:
                best_text = select_ocr_page(candidates)
                base = _select_ocr_candidate(candidates)
                consensus_span.set_attribute(
                    "ocr.selected.engine", base.engine,
                )
                consensus_span.set_attribute("ocr.selected.dpi", base.dpi)
                consensus_span.set_attribute("ocr.selected.psm", base.psm)
            if pass_metrics is not None:
                page_metrics = [metric for _, metric, _ in page_tracked]
                consensus_lines = [line for line in best_text.splitlines() if line]
                for candidate, metric in zip(candidates, page_metrics):
                    metric["selected_base"] = candidate is base
                    coverage = sum(
                        any(_ocr_lines_match(line, candidate_line.text) for candidate_line in candidate.lines)
                        for line in consensus_lines
                    )
                    ratio = round(coverage / len(consensus_lines), 4) if consensus_lines else 0.0
                    metric["consensus_line_coverage"] = coverage
                    metric["consensus_coverage_ratio"] = ratio
                    metric["quality"]["consensus_line_coverage"] = coverage
                    metric["quality"]["consensus_coverage_ratio"] = ratio
                for tracked in page_tracked:
                    telemetry.finish_ocr_pass(*tracked)
                    unfinished_passes.remove(tracked)
            pages.append(best_text)
            telemetry.finish_operation(
                page_span, {"receipt.page.status": "completed"},
            )
            page_span = None
    except BaseException as exc:
        if page_span is not None:
            telemetry.finish_operation(page_span, {"receipt.page.status": "failed"}, exc)
        raise
    finally:
        for tracked in unfinished_passes:
            telemetry.finish_ocr_pass(*tracked)
    return pages


def _ocr_pdf_pages(
    path: Path,
    pass_metrics: Optional[List[dict]] = None,
    *,
    run_uuid: str | None = None,
) -> List[str]:
    try:
        import pypdfium2 as pdfium  # type: ignore
    except Exception:
        return []
    doc = pdfium.PdfDocument(str(path))
    try:
        return _ocr_rendered_pages(
            lambda index, dpi: doc[index].render(scale=dpi / 72).to_pil(),
            len(doc), pass_metrics, run_uuid=run_uuid,
        )
    finally:
        doc.close()


def _ocr_image_pages(
    path: Path,
    pass_metrics: Optional[List[dict]] = None,
    *,
    run_uuid: str | None = None,
) -> List[str]:
    from PIL import Image, ImageOps

    with Image.open(path) as document:
        source_pages = {}

        def render_page(index: int, dpi: int):
            document.seek(index)
            image = ImageOps.exif_transpose(document).convert("RGB")
            native_dpi = document.info.get("dpi")
            try:
                x_dpi, y_dpi = (float(value) for value in native_dpi)
                if not (36 <= x_dpi <= 2400 and 36 <= y_dpi <= 2400):
                    raise ValueError("Implausible image resolution")
                basis = "image_metadata"
            except (TypeError, ValueError):
                x_dpi = y_dpi = 300.0
                basis = "assumed_300_dpi_without_usable_metadata"
            source_pages[index + 1] = {
                "pixel_width": image.width, "pixel_height": image.height,
                "native_dpi_x": x_dpi, "native_dpi_y": y_dpi, "dpi_basis": basis,
            }
            size = (max(1, round(image.width * dpi / x_dpi)),
                    max(1, round(image.height * dpi / y_dpi)))
            return image.resize(size, Image.Resampling.LANCZOS)

        first_metric = len(pass_metrics) if pass_metrics is not None else 0
        try:
            return _ocr_rendered_pages(
                render_page, getattr(document, "n_frames", 1), pass_metrics, run_uuid=run_uuid,
            )
        finally:
            if pass_metrics is not None:
                for metric in pass_metrics[first_metric:]:
                    metric["provenance"]["source_image"] = source_pages.get(metric["page_number"], {})


def _supplement_missing_receipt_summary(primary: str, alternative: str) -> str:
    """Add only missing summary/payment lines from another OCR layer."""
    if _looks_like_tesseract_tsv(alternative):
        return primary
    values = list(extract_totals(primary, infer_tax=False))
    original = tuple(values)
    components = extract_tax_components(primary)
    payment = extract_payment_provenance(primary)
    additions: List[str] = []
    for raw_line in _summary_lines(alternative):
        line = normalize_line(raw_line)
        if not line or line in primary:
            continue
        candidate = extract_totals(line)
        candidate_payment = extract_payment_provenance(line)
        supplied = False
        line_components = extract_tax_components(line)
        new_components = {code: amount for code, amount in line_components.items() if code not in components}
        target_tax = values[1] if not components else None
        if values[0] is not None and values[2] is not None and values[0] * values[2] > 0:
            target_tax = round(values[2] - values[0], 2)
        if new_components and target_tax is not None:
            tentative = round(sum(components.values()) + sum(new_components.values()), 2)
            if tentative * target_tax < 0 or abs(tentative) > abs(target_tax) + .01:
                continue
        for code, amount in line_components.items():
            if code not in components:
                components[code] = amount
                supplied = True
        for index, value in enumerate(candidate):
            if values[index] is None and value is not None:
                values[index] = value
                supplied = True
        if payment.payment_method is None and candidate_payment.payment_method:
            payment = candidate_payment
            supplied = True
        elif payment.card_last4 is None and candidate_payment.card_last4:
            payment = candidate_payment
            supplied = True
        if supplied:
            additions.append(line)
    result = primary + (("\n" + "\n".join(additions)) if additions else "")
    if (all(value is not None for value in original)
            and abs(round(original[0] + original[1] - original[2], 2)) <= .01
            and extract_totals(result, infer_tax=False) != original):
        return primary
    return result


def extract_page_text(
    path: Path,
    pass_metrics: Optional[List[dict]] = None,
    *,
    run_uuid: str | None = None,
) -> List[str]:
    if path.suffix.lower() == ".pdf":
        embedded_pages: List[str] = []
        image_based = False
        try:
            import pdfplumber  # type: ignore
            with pdfplumber.open(str(path)) as pdf:
                embedded_pages = [_usable_embedded_text(page.extract_text() or "") for page in pdf.pages]
                image_based = any(page.images for page in pdf.pages)
        except Exception:
            pass
        if image_based or not any(page.strip() for page in embedded_pages):
            ocr_pages = _ocr_pdf_pages(path, pass_metrics, run_uuid=run_uuid)
            if ocr_pages:
                selected: List[str] = []
                for idx in range(max(len(embedded_pages), len(ocr_pages))):
                    embedded = embedded_pages[idx] if idx < len(embedded_pages) else ""
                    ocr = ocr_pages[idx] if idx < len(ocr_pages) else ""
                    # A valid timestamp is strong evidence that preprocessing
                    # recovered receipt text the embedded OCR layer damaged.
                    if _has_literal_valid_timestamp(ocr) and not _has_literal_valid_timestamp(embedded):
                        selected.append(_supplement_missing_receipt_summary(ocr, embedded))
                    else:
                        selected.append(_supplement_missing_receipt_summary(embedded or ocr, ocr if embedded else ""))
                return selected
        return embedded_pages
    return _ocr_image_pages(path, pass_metrics, run_uuid=run_uuid)


def _align_page_layout(text: str, detected: Sequence[OCRLine]) -> List[OCRLine]:
    """Attach geometry from a layout pass to the final consensus text."""
    aligned: List[OCRLine] = []
    unused = list(detected)
    for raw_line in text.splitlines():
        line = normalize_line(raw_line)
        if not line or _looks_like_tesseract_tsv(line):
            continue
        matches = [candidate for candidate in unused if _ocr_lines_match(line, candidate.text)]
        if matches:
            match = max(matches, key=lambda candidate: SequenceMatcher(None, _line_key(line), _line_key(candidate.text)).ratio())
            unused.remove(match)
            aligned.append(OCRLine(line, match.confidence, match.x, match.y, match.width, match.height))
        else:
            aligned.append(OCRLine(line, 0.0))
    return aligned


def extract_page_layout(path: Path, pages: Sequence[str]) -> List[List[OCRLine]]:
    """Run one geometry pass and align it with already-selected page text."""
    detected_pages: List[Tuple[OCRLine, ...]] = []
    if path.suffix.lower() == ".pdf":
        try:
            import pypdfium2 as pdfium  # type: ignore

            doc = pdfium.PdfDocument(str(path))
            try:
                for index in range(len(doc)):
                    image = doc[index].render(scale=300 / 72).to_pil()
                    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                        tmp_path = Path(tmp.name)
                    try:
                        image.save(tmp_path)
                        detected_pages.append(_run_tesseract_candidate(tmp_path, dpi=300, psm="6").lines)
                    finally:
                        tmp_path.unlink(missing_ok=True)
            finally:
                doc.close()
        except Exception:
            detected_pages = []
    else:
        try:
            from PIL import Image

            with Image.open(path) as image:
                prepared = _prepare_ocr_image(image)
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                    tmp_path = Path(tmp.name)
                try:
                    prepared.save(tmp_path)
                    detected_pages.append(_run_tesseract_candidate(tmp_path, dpi=0, psm="6").lines)
                finally:
                    tmp_path.unlink(missing_ok=True)
        except Exception:
            detected_pages = []
    return [
        _align_page_layout(page, detected_pages[index] if index < len(detected_pages) else ())
        for index, page in enumerate(pages)
    ]


def normalize_line(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip()


def _line_key(line: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", line.lower()).strip()


def merge_page_text(pages: Sequence[str], max_overlap_lines: int = 25) -> str:
    merged: List[str] = []
    for page in pages:
        lines = [normalize_line(x) for x in page.splitlines() if normalize_line(x)]
        if not merged:
            merged.extend(lines)
            continue
        limit = min(max_overlap_lines, len(merged), len(lines))
        overlap = 0
        for n in range(limit, 0, -1):
            if [_line_key(x) for x in merged[-n:]] == [_line_key(x) for x in lines[:n]]:
                overlap = n
                break
        if not overlap:
            # Scanner page overlap can repeat a department heading with OCR
            # damage. Require two matching purchase rows; never deduplicate
            # identical purchases within a page or guess from one fuzzy row.
            for offset in (0, 1):
                if offset and (not lines[0].isupper() or parse_money(lines[0]) is not None):
                    continue
                for n in range(min(limit, len(lines) - offset), 1, -1):
                    left, right = merged[-n:], lines[offset:offset+n]
                    products = sum(parse_money(x) is not None and not TOTAL_WORDS.search(x)
                                   and not NON_ITEM_WORDS.search(x) and '@' not in x for x in left)
                    if products >= 2 and all(_ocr_lines_match(a, b) for a, b in zip(left, right)):
                        overlap = offset + n
                        break
                if overlap:
                    break
        merged.extend(lines[overlap:])
    return "\n".join(merged)


def parse_money(line: str) -> Optional[float]:
    line = normalize_leading_decimal_money(line)
    matches = list(MONEY_RE.finditer(line))
    if not matches:
        return None
    m = matches[-1]
    value = float(m.group(1).replace(",", ""))
    token = m.group(0).strip()
    return -value if token.startswith("-") or token.endswith("-") else value


def normalize_leading_decimal_money(line: str) -> str:
    """Restore a zero in standalone OCR amounts such as ``@ .01-``.

    The non-numeric prefix requirement deliberately leaves damaged values such
    as ``42 .54-`` for the tolerant semantic parser instead of turning them
    into ``0.54-``.
    """
    return re.sub(
        r"(^|[^\d\s])(\s*)\.(\d{2})(-?)",
        lambda match: f"{match.group(1)}{match.group(2)}0.{match.group(3)}{match.group(4)}",
        line,
    )


def parse_money_tolerant(line: str) -> Optional[float]:
    """Parse OCR-damaged money only after the caller has anchored the line semantically."""
    matches = list(OCR_MONEY_RE.finditer(line))
    if not matches:
        return None
    m = matches[-1]
    value = float(f"{m.group('whole')}.{m.group('cents')}")
    return -value if m.group("lead") or m.group("trail") else value


def extract_date(text: str) -> Optional[str]:
    from .datetime import date_part, extract_transaction_datetime
    return date_part(extract_transaction_datetime(text))


def extract_receipt_id(text: str) -> Optional[str]:
    transaction_id = None
    for line in text.splitlines():
        for pattern in ID_PATTERNS:
            m = pattern.search(line)
            if m:
                if re.match(r'trans(?:action)?\b', m.group(0), re.I):
                    transaction_id = transaction_id or m.group(1)
                else:
                    return m.group(1)
    # Printed transaction codes may group digits for readability. Require the
    # explicit TC# label and a numeric-only row so an EMV "TC <hex>" payment
    # cryptogram is never used as the receipt identifier.
    for line in text.splitlines():
        match = re.fullmatch(r"\s*TC\s*#\s*((?:\d[ \t-]*){4,64})\s*", line, re.I)
        if match:
            return re.sub(r"\D", "", match[1])
    return transaction_id


def normalize_merchant(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    line = normalize_line(raw).strip("-_=|.,:; ")
    for pattern, replacement in MERCHANT_ALIASES:
        if pattern.search(line):
            if replacement is not None:
                return replacement
            return re.sub(r"^.*?sobeys", "Sobeys", line, flags=re.I)
    return line or None


def _merchant_name_from_store_header(text: str, header: str) -> Optional[str]:
    """Recover a merchant spelling from repeated, similar words on a receipt."""
    marker = re.search(r"\b(?:store|location|branch)\b", header, re.I)
    prefix = header[:marker.start()] if marker else header
    prefix_words = re.findall(r"[A-Za-z][A-Za-z'’]{3,}", prefix)
    if not prefix_words:
        suffix = re.match(r"\s*[:#]?\s*[A-Z]*\d[\w-]*\s+(.+)", header[marker.end():], re.I) if marker else None
        if suffix and not re.search(r"\b(?:register|terminal|trans(?:action)?|cashier)\b", suffix[1], re.I):
            return normalize_merchant(suffix[1])
        return None
    anchor = max(prefix_words, key=len)
    anchor_norm = re.sub(r"[^a-z]", "", anchor.lower())
    variants: Counter[str] = Counter()
    display: dict[str, str] = {}
    for word in re.findall(r"[A-Za-z][A-Za-z'’]{3,}", text):
        normalized = re.sub(r"[^a-z]", "", word.lower().removesuffix("s") if word.lower().endswith("'s") else word.lower())
        if normalized and SequenceMatcher(None, anchor_norm, normalized).ratio() >= 0.72:
            variants[normalized] += 1
            display.setdefault(normalized, word.strip("'’"))
    if variants:
        winner, support = max(variants.items(), key=lambda item: (item[1], SequenceMatcher(None, anchor_norm, item[0]).ratio()))
        if support >= 2:
            return display[winner]
    return anchor


def _merchant_address_line(line: str) -> bool:
    """Reject location/contact metadata when considering a retailer name."""
    return bool(
        re.search(r"\b\d{1,6}\b.*\b(?:rd|road|street|st|ave|avenue|blvd|boulevard|drive|dr|lane|ln|unit|suite)\b", line, re.I)
        or re.search(r",\s*[A-Z]{2}\s+[A-Z]\d[A-Z][A-Z0-9 ]{3,}\b", line)
        or re.match(r"^(?:telephone|phone|fax|tel)\b", line, re.I)
    )


def _merchant_logo(line: str) -> Optional[str]:
    line = normalize_line(line).strip(" \"'“”‘’~_=|.,:;-")
    if re.fullmatch(r"[A-Za-z]{1,4}\s*[&+]\s*[A-Za-z]{1,4}\.?", line):
        return re.sub(r"\s+", "", line).rstrip('.')
    return None


def merchant_from_observations(text: str, observations) -> Optional[str]:
    """Require repeated header readings and matching printed website evidence."""
    readings = []
    texts = [text]
    for observation in observations or ():
        if not isinstance(observation, dict):
            continue
        value = observation.get('text') or ''
        if not isinstance(value, str):
            continue
        if (observation.get('page_number') != 1
                or observation.get('status') not in (None, 'success')
                or not value or _looks_like_tesseract_tsv(value)):
            continue
        texts.append(value)
        if merchant := extract_merchant(value):
            readings.append(merchant)
    # A footer URL corroborates a retailer already read in the header; it never
    # introduces an otherwise unobserved merchant or a payment-card partner.
    hosts = re.findall(r'(?:https?://(?:www\s*\.\s*)?|www\s*[.,])\s*([A-Za-z0-9-]+)\s*\.', '\n'.join(texts), re.I)
    keys = {_line_key(host) for host in hosts}
    counts = Counter(_line_key(name) for name in readings if _line_key(name) in keys)
    if not counts:
        return None
    best = counts.most_common()
    if best[0][1] < 2 or len(best) > 1 and best[0][1] == best[1][1]:
        return None
    return next(name for name in readings if _line_key(name) == best[0][0])


def extract_merchant(text: str) -> Optional[str]:
    header_lines = text.splitlines()[:12]
    # Email PDFs can repeat each header glyph in overlapping text layers.
    # Read only the explicit subject issuer, never To/From addresses.
    subject = re.compile(r'^\s*S+U+B+J+E+C+T+\s*:+\s*(.+)$',re.I)
    for raw in header_lines:
        match = subject.match(raw)
        if match and (issuer := merchant_title_hint(match[1])):
            return issuer
    # Browser exports can prepend a timestamp and page title. Keep only the
    # title's merchant when its spelling is supported by the document URL.
    hosts = re.findall(r'(?:https?://|www\.)([A-Za-z0-9.-]+)', text, re.I)
    host_labels = {re.sub(r'[^a-z0-9]', '', host.lower().removeprefix('www.').split('.')[0]) for host in hosts}
    for raw in header_lines:
        if '|' in raw:
            name = normalize_line(raw.rsplit('|', 1)[1])
            if re.search(r'[A-Za-z]{3}', name) and re.sub(r'[^a-z0-9]', '', name.lower()) in host_labels:
                return normalize_merchant(name)
    # Prefer a recognized merchant anywhere in the header over a preceding
    # slogan or tagline.
    for raw in header_lines:
        line = normalize_line(raw)
        for pattern, replacement in MERCHANT_ALIASES:
            if pattern.search(line):
                return replacement or normalize_merchant(line)
    for raw in header_lines:
        if logo := _merchant_logo(raw):
            return logo
    for raw in header_lines:
        line = normalize_line(raw)
        if re.search(r"\b(?:store|location|branch)\s*[:#]?\s*[A-Z]*\d[\w-]*\b", line, re.I) and re.search(r"[A-Za-z]{3}", line):
            merchant = _merchant_name_from_store_header(text, line)
            if merchant:
                return merchant
    # A closing acknowledgement can corroborate a name already visible in the
    # header. Never introduce a merchant found only in footer advertising.
    for match in re.finditer(r"\bthank\s+you\s+for\s+shopping\s+(?:at|with)\s+([^,\n.!]+)", text, re.I):
        name = normalize_line(match.group(1)).strip()
        if re.fullmatch(r"(?:our|this|the)\s+store", name, re.I):
            continue
        for raw in header_lines:
            line = normalize_line(raw)
            if _line_key(line) == _line_key(name) and re.search(r"[A-Za-z]{3}", name):
                return normalize_merchant(line)
    for raw in header_lines:
        line = normalize_line(raw)
        business_suffix = re.search(r"\b(?:co-?op(?:erative)?|incorporated|inc|limited|ltd|llc|corporation)\.?$", line, re.I)
        if (not MONEY_RE.search(line) and len(line) <= 70
                and business_suffix
                and len(re.sub(r'[^A-Za-z]', '', line[:business_suffix.start()])) >= 3):
            return normalize_merchant(line)
    for raw in header_lines:
        line = normalize_line(raw)
        if re.search(r'\b(?:ship\s+to|bill\s+to|sold\s+to)\b',line,re.I):
            break
        if (not line or MONEY_RE.search(line) or len(line) > 70
                or _merchant_address_line(line)
                or re.match(r'^\d{3,14}\s+.*[A-Za-z]', line)
                or '@' in line
                or not re.search(r'[A-Za-z]{4}', line) and not re.fullmatch(r'[A-Z]{3}', line)):
            continue
        if re.search(r"[A-Za-z]{3}", line) and not re.search(r"\b(receipt|invoice|date|time|tel|telephone|phone|store|register|reg|price|discount|location|branch|cashier|customer|loyalty|member|purchases?|orders?|transaction|subtotal|total|gst|pst|hst|gift\s+cards?|thank\s+you|delivered|delivery|subject|payment)\b", line, re.I):
            return normalize_merchant(line)
    return None


def merchant_title_hint(title: str) -> Optional[str]:
    name = ''
    if '|' in title:
        name = title.rsplit('|',1)[1]
    else:
        match = re.match(r'^Your\s+(.+?)\s+(?:receipt|invoice)\b',title,re.I)
        if match:
            name = match[1]
    name = normalize_line(name).strip()
    if (len(name) <= 100 and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 &'’.-]{2,}",name)
            and len(re.findall(r'[A-Za-z]',name)) >= 3
            and not re.fullmatch(r'(?:receipt|invoice|orders?|purchases?|payment|cash|card|pdf)',name,re.I)):
        return normalize_merchant(name)
    return None


def pdf_merchant_hint(path: Path) -> Optional[str]:
    """Read an issuer from a browser-export title, preserving all OCR text."""
    filename = path.stem
    filename_hint = merchant_title_hint(filename if filename.lower().startswith('your ') else 'Your '+filename)
    if path.suffix.lower() != '.pdf':
        return filename_hint
    try:
        import pypdfium2 as pdfium
        with pdfium.PdfDocument(path) as document:
            title = document.get_metadata_dict().get('Title') or ''
    except Exception:
        return filename_hint
    return merchant_title_hint(title) or filename_hint


def extract_merchant_store(text: str, merchant_hint: Optional[str] = None, *, observations=()) -> Tuple[Optional[str], Optional[str]]:
    """Separate the retailer from printed branch/location evidence.

    A merchant hint is existing canonical/source metadata, never a guess from a
    co-branded payment card. Store labels and retailer suffixes remain distinct.
    """
    merchant = merchant_from_observations(text, observations) or extract_merchant(text)
    header = [normalize_line(line) for line in text.splitlines()[:20]]
    stores = {}
    for line in header:
        for match in re.finditer(r'\b(?:(?:store|location|branch)\s*[:#]?|ST\s*#)\s*(\d{1,8})\b', line, re.I):
            key = str(int(match[1]))
            stores.setdefault(key, 'Store ' + match[1])
        if (re.fullmatch(r"[A-Za-z][A-Za-z .'-]*\s+#\d{1,8}", line)
                and not re.search(r'\b(?:receipt|invoice|member|loyalty|card|tax|gst|pst|hst|total|auth|trans|order|ticket|tc)\b', line, re.I)):
            number = re.search(r'#(\d+)', line)[1]
            stores.setdefault(str(int(number)), line)
    store = next(iter(stores.values())) if len(stores) == 1 else None
    if merchant and store and _line_key(merchant) == _line_key(store):
        merchant = merchant_hint if _line_key(merchant_hint or '') != _line_key(store) else None
    elif not merchant and merchant_hint and _line_key(merchant_hint) != _line_key(store or ''):
        merchant = merchant_hint
    if merchant:
        for pattern, replacement in MERCHANT_ALIASES:
            match = pattern.search(merchant)
            if match:
                remainder = merchant[match.end():].strip(' #:-')
                if remainder and not store and not stores:
                    store = merchant
                merchant = replacement or normalize_merchant(match[0])
                break
        # Merchant corroboration may identify a separate logo rather than the
        # branch header. Retain a complete printed merchant/location line when
        # the following street address establishes that it is a store header.
        labels = []
        for reading in _store_header_readings(text, observations):
            header = _store_header_lines(reading)
            for index, line in enumerate(header):
                if (line.casefold().startswith(merchant.casefold() + ' ')
                        and not MONEY_RE.search(line) and not _merchant_address_line(line)
                        and any(_store_street_line(row) for row in header[index + 1:index + 3])):
                    labels.append(line)
            if labels and reading == text and len(stores) <= 1 and len(set(labels)) == 1:
                return merchant, labels[0]
        unique = {_line_key(label): label for label in labels}
        if len(unique) == 1 and len(stores) <= 1:
            store = next(iter(unique.values()))
    return merchant, store


def _store_header_lines(text):
    result = []
    for raw in text.splitlines()[:12]:
        line = normalize_line(raw)
        if re.match(r'^(?:bill\s*to\b|ship\s*to\b|sold\s*to\b|customer\s+(?:name|address)\b|member\b|loyalty\b|served\s+by\b|subtotal\b|total(?:\s*[:$]|\s+\d|\s*$))', line, re.I):
            break
        result.append(line)
    return result


def _store_street_line(line):
    return not MONEY_RE.search(line) and bool(re.fullmatch(
        r"(?:\d{1,5}\s*[-/]\s*)?\d{1,6}[A-Za-z]?\s+[^@\n]{2,100}\b"
        r"(?:street|st|avenue|ave|road|rd|boulevard|blvd|drive|dr|lane|ln|way|court|ct|parkway|pkwy|highway|hwy|crescent|cres|place|pl|trail)"
        r"\.?\s*(?:[NSEW]{1,2}|(?:unit|suite)\s*#?\s*[A-Za-z0-9-]+)?", line, re.I))


def _store_header_readings(text, observations):
    yield text
    for observation in observations or ():
        if not isinstance(observation, dict):
            continue
        if observation.get('page_number') != 1 or observation.get('status') not in (None, 'success'):
            continue
        provenance = observation.get('provenance') or {}
        if isinstance(provenance, str):
            provenance = json.loads(provenance)
        reading = observation.get('text') or observation.get('extracted_text') or '\n'.join(
            row['text'] for row in provenance.get('lines', []) if isinstance(row, dict) and isinstance(row.get('text'), str))
        if isinstance(reading, str) and reading and not _looks_like_tesseract_tsv(reading):
            yield reading


def extract_store_address(text, *, observations=()):
    """Read a printed street in the store header; never a customer address."""
    alternatives = {}
    for reading in _store_header_readings(text, observations):
        addresses = [line for line in _store_header_lines(reading) if _store_street_line(line)]
        if len(addresses) == 1:
            if reading == text:
                return addresses[0]
            alternatives[_line_key(addresses[0])] = addresses[0]
        elif len(addresses) > 1:
            return None
    return next(iter(alternatives.values())) if len(alternatives) == 1 else None


def _tax_code(line: str) -> Optional[str]:
    codes = set(re.findall(r'\b(?:gst|pst|rst|hst|qst)\b', line.lower()))
    if codes and codes <= {'gst', 'hst'}:
        return 'gst'
    if codes and codes <= {'pst', 'rst', 'qst'}:
        return 'pst'
    return None


def _tax_amount_text(line: str) -> str:
    # Remove identifier/rate tokens, retaining an adjacent printed money value.
    line = re.sub(r'(\b(?:gst|pst|rst|hst|qst))\s*(?:#|registration|reg\b|number)\s*[:#]?\s*[A-Z0-9_.-]+', r'\1', line, flags=re.I)
    line = re.sub(r'\b[A-Z]{1,6}\d{6,}[A-Z0-9]*\b', '', line, flags=re.I)
    return re.sub(r'\b\d+(?:\.\d+)?\s*%', '', line)


def _summary_lines(text: str) -> List[str]:
    """Associate a standalone financial label only with its adjacent amount."""
    lines = text.splitlines()
    result = []
    index = 0
    label = re.compile(r'^(?:sub\s*total|total(?:\s+tax)?|tax|(?:[A-Z]\s+)?(?:gst|pst|rst|hst|qst)(?:\s*/\s*(?:gst|pst|rst|hst|qst))?(?:\s*\([A-Z]{2,3}\))?)\s*[:=-]?$', re.I)
    amount = re.compile(r"^[-(]?\$?\s*\d[\d ,.'’]*[.)-]?\s*[A-Z]{0,2}$", re.I)
    while index < len(lines):
        line = normalize_line(lines[index])
        match_line = normalize_line(_tax_amount_text(line)) if re.search(r'\b(?:gst|pst|rst|hst|qst)\b', line, re.I) else line
        if label.fullmatch(match_line) and index + 1 < len(lines):
            following = normalize_line(lines[index + 1])
            # Tax rates are not money. A rate-only row may separate the printed
            # tax label from its amount; never cross an identifier or other row.
            if (re.search(r'\b(?:gst|pst|rst|hst|qst)\b', line, re.I)
                    and re.fullmatch(r'\d+(?:\.\d+)?\s*%', following)
                    and index + 2 < len(lines)):
                following_amount = normalize_line(lines[index + 2])
                if amount.fullmatch(following_amount) and parse_money(following_amount) is not None:
                    result.append(line + ' ' + following + ' ' + following_amount)
                    index += 3
                    continue
            if amount.fullmatch(following) and parse_money_tolerant(following) is not None:
                result.append(line + ' ' + following)
                index += 2
                continue
        result.append(line)
        index += 1
    return result


def extract_totals(text: str, *, infer_tax: bool = True) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    subtotal = tax = total = None
    taxes = {}
    included_tax = False
    total_candidates: List[Tuple[float, bool]] = []
    tender_candidates: List[Tuple[float, bool]] = []
    for raw in _summary_lines(text):
        line = normalize_line(raw)
        total_like = bool(TOTAL_LIKE_RE.search(line))
        subtotal_like = bool(re.search(r"\b(?:sub\s*tota[l1]?|suptotal)\b", line, re.I))
        tax_like = bool(re.search(r"\b(?:total\s+tax|tax|gst|pst|rst|hst|qst)\b", line, re.I))
        tender_like = bool(TENDER_LINE_RE.search(line)) and not re.search(
            r"\b(?:change|auth|reference|card\s+number)\b", line, re.I
        )
        amount_line = _tax_amount_text(line) if tax_like else line
        amount = parse_money(amount_line)
        tolerant = False
        if amount is None and (subtotal_like or total_like or tender_like or tax_like):
            amount = parse_money_tolerant(amount_line)
            tolerant = amount is not None
        if amount is None:
            continue
        if subtotal_like:
            subtotal = amount
            continue
        if tax_like:
            if re.search(r'\b(?:inc|incl|included)\b', line, re.I):
                included_tax = True
                continue
            key = _tax_code(line) or 'tax'
            # Repeated receipt summaries are alternate observations, not
            # additional taxes. Prefer named components over their summary.
            taxes[key] = amount
            continue
        if total_like:
            if not re.search(r"\b(?:tax|items?|savings?|discount|points?|purchased)\b", line, re.I):
                total_candidates.append((amount, tolerant))
                continue
        if tender_like:
            tender_candidates.append((amount, tolerant))

    if total_candidates:
        total, total_tolerant = total_candidates[-1]
        if tender_candidates:
            tender, tender_tolerant = tender_candidates[-1]
            if total_tolerant and tender != 0 and abs(total) > abs(tender) * 2:
                total = tender
            elif total_tolerant and not tender_tolerant and abs(abs(total) - abs(tender)) <= 1.0:
                total = tender
    elif tender_candidates:
        total = tender_candidates[-1][0]
    components = [value for key, value in taxes.items() if key != 'tax']
    tax = round(sum(components), 2) if components else taxes.get('tax', 0.0 if included_tax else None)
    if subtotal is None and included_tax and total is not None:
        subtotal = round(total - (tax or 0), 2)
    if infer_tax and subtotal is not None and total is not None and subtotal * total > 0:
        derived_tax = round(abs(total) - abs(subtotal), 2)
        if 0 <= derived_tax <= max(0.01, abs(subtotal) * 0.30) and (tax is None or abs(tax) < derived_tax):
            tax = derived_tax if total > 0 else -derived_tax
    return subtotal, tax, total


def extract_tax_components(text: str) -> dict[str, float]:
    """Read printed named taxes; rates and registration numbers are not amounts."""
    components = {}
    for line in _summary_lines(text):
        code = _tax_code(line)
        if code is None or re.search(r'\b(?:inc|incl|included)\b', line, re.I):
            continue
        amount_line = _tax_amount_text(line)
        amount = parse_money(amount_line)
        if amount is None:
            amount = parse_money_tolerant(amount_line)
        if amount is not None:
            components[code] = amount
    return components


def extract_items(text: str) -> List[ScannedItem]:
    items: List[ScannedItem] = []
    raw_lines = text.splitlines()
    for index, raw in enumerate(raw_lines):
        line = normalize_leading_decimal_money(normalize_line(raw))
        if re.match(r'^(?:sub\s*tota[l1]?|suptotal)\b', line, re.I) or (
                re.match(r'^(?:total|tax\s+summary)\b', line, re.I)
                and (items or parse_money(line) is not None)):
            break
        # Checkout timestamps/register/transaction identifiers are metadata,
        # even when OCR punctuation makes an identifier resemble a price.
        if (re.search(r'\b\d{1,2}:\d{2}', line)
                and re.search(r'\b(?:reg(?:ister)?|trans(?:action)?)\b', line, re.I)):
            continue
        adjustment = re.match(r'^(?:INSTANT\s+SAVINGS|\+?EHC)\b', line, re.I)
        if adjustment:
            amount = parse_money(line)
            if items and amount is not None:
                direction = -1 if items[-1].line_total < 0 else 1
                delta = -abs(amount) if 'savings' in line.lower() else abs(amount)
                items[-1].line_total = round(items[-1].line_total + direction * delta, 2)
            continue
        if re.match(r'^(?:price\s*/|lit(?:er|re)s?\s*:|pump\s*[:\d]|fiscal\s+\d{4}\s+loyalty)', line, re.I) or re.search(r'loyalty\s+appeasement', line, re.I):
            continue
        if not line or TOTAL_WORDS.search(line) or NON_ITEM_WORDS.search(line):
            continue
        if re.match(r'^(?:amount(?:\s+(?:paid|charged))?|payment|tender(?:ed)?|auth(?:orization)?|reference)\s*[:#-]?(?:\s|\$|$)', line, re.I):
            continue
        if re.search(r"^\s*(?:\d+(?:\.\d+)?\s*(?:kg|lb)\s*@|\d+\s*@\s*\d+\s*/)", line, re.I):
            continue
        matches = list(MONEY_RE.finditer(line))
        if not matches:
            if re.match(r'^\s*(?:\d+\s+)?(?:regular|premium|diesel|unleaded)\s+(?:gasoline|fuel)\b', line, re.I):
                for candidate in raw_lines[index+1:index+9]:
                    candidate = normalize_line(candidate)
                    if TOTAL_WORDS.search(candidate):
                        break
                    if re.fullmatch(r'[\d$.,\s-]+', candidate) and len(MONEY_RE.findall(candidate)) >= 2:
                        amount = parse_money(candidate)
                        if amount is not None:
                            items.append(ScannedItem(re.sub(r'^\d+\s+', '', line), amount))
                            break
            # Some OCR splits a barcode/item line from its amount column.
            # Only join strong product rows so arbitrary receipt prose is not
            # turned into a line item.
            if re.match(r"^\d{3,14}\s+.*[A-Za-z]", line) and index + 1 < len(raw_lines):
                next_line = normalize_line(raw_lines[index + 1])
                next_matches = list(MONEY_RE.finditer(next_line))
                if next_matches and re.fullmatch(r'[-(]?\$?\s*\d[\d ,.]*[.)-]?\s*[A-Z]{0,2}', next_line, re.I):
                    amount = parse_money(next_line)
                    if amount is not None:
                        items.append(ScannedItem(line, amount))
            continue
        last = matches[-1]
        name = clean_extracted_item_name(line[: last.start()].strip(" :-"))
        if re.match(r'^\s*(?:\d+\s+)?(?:regular|premium|diesel|unleaded)\b', line, re.I) and len(matches) >= 2:
            name = re.sub(r'^\d+\s+', '', line[:matches[0].start()].strip())
        if re.match(r'^\d{6,14}\s+[1IlT](?:\s*ea)?\s+', line, re.I) and index:
            previous = normalize_line(raw_lines[index - 1])
            if parse_money(previous) is None and len(re.findall(r'[A-Za-z]', previous)) >= 3 and not TOTAL_WORDS.search(previous) and not NON_ITEM_WORDS.search(previous):
                name = previous
        if len(re.sub(r"[^A-Za-z]", "", name)) < 2:
            continue
        amount = parse_money(line)
        if amount is not None:
            items.append(ScannedItem(name, amount))
    return items


def clean_extracted_item_name(name: str) -> str:
    """Remove checkout metadata that OCR commonly attaches to product names."""
    cleaned = normalize_line(name)
    # Retail rows commonly end in ``<UPC> <quantity> @ <unit price>``. The
    # price is removed by extract_items; remove the remaining UPC/quantity
    # columns as well, including a short OCR fragment before a damaged UPC.
    cleaned = re.sub(r"\s+(?:[A-Za-z0-9]{1,3}\s+)?\d[\d ]{6,16}\s+\d+\s+@\s*$", "", cleaned)
    # This Cricut abbreviation is especially unstable in the Michaels scan
    # (CRCT/EReT and IO/1]/12), while PUFF and its 12x1 size remain legible.
    puff = re.search(r"\bPUFF\s+12[xX][1!)](?=\s|$)", cleaned, re.I)
    if puff and re.match(r"^(?:CRCT|ERET)\b", cleaned, re.I):
        cleaned = "CRCT " + re.sub(r"[!)]$", "1", puff.group(0), flags=re.I)
    return normalize_line(cleaned.strip(" :-"))


def normalize_item_signs(items: List[ScannedItem], total: Optional[float]) -> List[ScannedItem]:
    """Apply a refund total's sign when OCR loses trailing item minus signs."""
    if total is not None and total < 0:
        return [ScannedItem(item.item_name, -abs(item.line_total), item.department) for item in items]
    return items


def confidence_for(receipt: ScannedReceipt) -> float:
    """Field coverage, not a probability that the OCR is correct."""
    score = 0.0
    score += 0.15 if receipt.merchant else 0.0
    score += 0.15 if receipt.transaction_date else 0.0
    score += 0.10 if receipt.receipt_id else 0.0
    score += 0.15 if receipt.subtotal is not None else 0.0
    score += 0.10 if receipt.tax is not None else 0.0
    score += 0.20 if receipt.total is not None else 0.0
    score += 0.15 if receipt.items else 0.0
    return round(score, 4)


def review_reasons_for(receipt: ScannedReceipt) -> List[str]:
    if not receipt.text.strip() or (not receipt.items and receipt.total is None and receipt.extraction_confidence <= 0.15):
        return ["unreadable_ocr"]
    reasons: List[str] = []
    reasons.extend('conflicting_ocr_' + name for name, decision in receipt.field_decisions.items()
                   if decision.get('status') == 'conflict')
    if not receipt.merchant:
        reasons.append("missing_merchant")
    observations = [p for p in (getattr(receipt, 'ocr_run', {}) or {}).get('ocr_passes', [])
                    if p.get('page_number') == 1 and p.get('text')
                    and p.get('status') in (None, 'success')]
    if receipt.merchant and len(observations) > 1:
        key = _line_key(receipt.merchant)
        support = sum(1 for p in observations
                      if (reading := extract_merchant_store(p['text'])[0])
                      and SequenceMatcher(None, key, _line_key(reading)).ratio() >= .85)
        if support < 2:
            reasons.append('uncorroborated_merchant')
    if not receipt.transaction_date:
        reasons.append("missing_date")
    if receipt.total is None:
        reasons.append("missing_total")
    if not receipt.items:
        reasons.append("missing_items")
    if receipt.extraction_confidence < 0.65:
        reasons.append("low_confidence")
    from .allocation import assess
    reasons.extend(assess([asdict(i) for i in receipt.items], receipt.subtotal,
                         receipt.tax, receipt.total)['reasons'])
    components = extract_tax_components(receipt.text)
    if (len(components) > 1 and receipt.tax is not None
            and abs(round(sum(components.values()) - receipt.tax, 2)) > .01):
        reasons.append('tax_components_mismatch')
    return reasons


def status_for(receipt: ScannedReceipt) -> str:
    reasons = review_reasons_for(receipt)
    if reasons == ["unreadable_ocr"]:
        return "unreadable"
    if reasons:
        return "review"
    return "complete"


def receipt_info_from_filename(path: Path) -> Tuple[Optional[str], Optional[str], Optional[float]]:
    """Extract date, merchant, and total from YYYYMMDD_merchant_dollars_cents."""
    match = re.fullmatch(r"(\d{8})[_-](.+)[_-](\d+)[_-](\d{2})", path.stem)
    if not match:
        return None, None, None
    raw_date, raw_merchant, dollars, cents = match.groups()
    try:
        parsed_date = datetime.strptime(raw_date, "%Y%m%d").date().isoformat()
    except ValueError:
        return None, None, None
    merchant = normalize_merchant(raw_merchant.replace("_", " ").replace("-", " "))
    total = float(f"{dollars}.{cents}")
    return parsed_date, merchant, total


def prefer_filename_merchant(extracted: Optional[str], filename_merchant: Optional[str]) -> Optional[str]:
    """Use filename evidence when OCR returns a generic receipt section label."""
    generic = {"grocery", "groceries", "market", "store", "supermarket"}
    if filename_merchant and (not extracted or normalize_line(extracted).lower() in generic):
        return filename_merchant
    return extracted


def parse_scan(path: Path, source_root: Optional[Path] = None) -> ScannedReceipt:
    from .datetime import allows_filename_date
    observations = []
    pages = extract_page_text(path, observations)
    text = merge_page_text(pages)
    merchant, store = extract_merchant_store(text, pdf_merchant_hint(path), observations=observations)
    subtotal, tax, total = extract_totals(text)
    filename_date, filename_merchant, filename_total = receipt_info_from_filename(path)
    payment = extract_payment_provenance(text)
    try:
        reference = path.relative_to(source_root).as_posix() if source_root and source_root.is_dir() else path.name
    except ValueError:
        reference = path.name
    receipt = ScannedReceipt(
        path=str(path), source_reference=reference, source_sha256=sha256_file(path),
        merchant=prefer_filename_merchant(merchant, filename_merchant),
        store=store,
        store_address=extract_store_address(text, observations=observations),
        transaction_date=extract_date(text) or (filename_date if allows_filename_date(text) else None),
        receipt_id=extract_receipt_id(text), subtotal=subtotal, tax=tax, total=total,
        payment_method=payment.payment_method, card_last4=payment.card_last4,
        items=normalize_item_signs(extract_items(text), total), page_text=list(pages), text=text,
    )
    from .parallel_ingest import _cache_layout_pages
    from .departments import assign_items
    from .store_contact import extract_store_contact
    receipt.ocr_layout = _cache_layout_pages(extract_page_layout(path, pages))
    assign_items(receipt.items, receipt.ocr_layout)
    for name, value in extract_store_contact(text, observations=observations).items():
        setattr(receipt, name, value)
    if receipt.total is None:
        receipt.total = filename_total
    from .summary_evidence import apply
    apply(receipt, observations)
    receipt.extraction_confidence = confidence_for(receipt)
    receipt.review_reasons = review_reasons_for(receipt)
    receipt.extraction_status = status_for(receipt)
    return receipt


def _db_connect(dsn: str):
    if os.getenv("HOME_BUDGET_PGPASSWORD") and not os.getenv("PGPASSWORD"):
        os.environ["PGPASSWORD"] = os.environ["HOME_BUDGET_PGPASSWORD"]
    try:
        import psycopg  # type: ignore
        return psycopg.connect(dsn or "dbname=home_budget")
    except ImportError:
        import psycopg2  # type: ignore
        return psycopg2.connect(dsn or "dbname=home_budget")


def upsert_receipt(conn, receipt: ScannedReceipt, schema: str = "budget") -> int:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        raise ValueError("invalid database schema")

    payment = extract_payment_provenance(receipt.text)
    if 'card_last4' in receipt.field_decisions:
        from .payment import PaymentProvenance
        payment = PaymentProvenance(payment.payment_method, receipt.field_decisions['card_last4']['value'])
    receipt.payment_method = payment.payment_method
    receipt.card_last4 = payment.card_last4
    receipt.payer = resolve_owner_from_db(conn, payment, receipt.transaction_date, schema)

    payload = {
        "store": receipt.store,
        "store_address": receipt.store_address,
        "store_phone": receipt.store_phone,
        "tax_registration_number": receipt.tax_registration_number,
        "items": [asdict(item) for item in receipt.items],
        "source_reference": receipt.source_reference,
        "source_sha256": receipt.source_sha256,
        "receipt_id": receipt.receipt_id,
        "payment_method": receipt.payment_method,
        "card_last4": receipt.card_last4,
        "payer": receipt.payer,
        "page_text": receipt.page_text,
        "ocr_text": receipt.text,
        "review_reasons": receipt.review_reasons,
        "field_decisions": receipt.field_decisions,
    }
    components = extract_tax_components(receipt.text)
    gst, pst = receipt.tax or 0, 0
    if components and abs(round(sum(components.values()) - (receipt.tax or 0), 2)) <= .01:
        gst = components.get('gst', 0) + components.get('hst', 0)
        pst = components.get('pst', 0) + components.get('rst', 0)
    payload['tax_components'] = components
    from ..categorization.receipt_items import load_existing, unmatched_reviewed, persist_items
    with conn.cursor() as cur:
        existing_pk, existing_items = load_existing(cur, receipt.canonical_order_id, schema)
        if unmatched_reviewed(receipt.items, existing_items):
            telemetry.LOG.warning('receipt_item_refresh_preserved reason=unmatched_reviewed_item')
            return existing_pk
        cur.execute(
            f"""
            INSERT INTO {schema}.expenses (
                source, order_id, receipt_filename, source_reference, source_sha256,
                order_date, store_name, receipt_item_subtotal, receipt_gst, receipt_pst,
                receipt_total_charged, expense_total, extraction_status,
                extraction_confidence, payment_method, card_last4, payer, raw_payload
            ) VALUES ('scanned', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source, order_id) DO UPDATE SET
                receipt_filename = EXCLUDED.receipt_filename,
                source_reference = EXCLUDED.source_reference,
                source_sha256 = EXCLUDED.source_sha256,
                order_date = CASE WHEN expenses.raw_payload ? 'receipt_date_correction'
                    THEN expenses.order_date ELSE EXCLUDED.order_date END,
                store_name = COALESCE(EXCLUDED.store_name, expenses.store_name),
                receipt_item_subtotal = EXCLUDED.receipt_item_subtotal,
                receipt_gst = EXCLUDED.receipt_gst,
                receipt_pst = EXCLUDED.receipt_pst,
                receipt_total_charged = EXCLUDED.receipt_total_charged,
                expense_total = EXCLUDED.expense_total,
                extraction_status = EXCLUDED.extraction_status,
                extraction_confidence = EXCLUDED.extraction_confidence,
                payment_method = EXCLUDED.payment_method,
                card_last4 = EXCLUDED.card_last4,
                payer = EXCLUDED.payer,
                raw_payload = CASE WHEN expenses.raw_payload ? 'receipt_date_correction'
                    THEN EXCLUDED.raw_payload || jsonb_build_object('receipt_date_correction',expenses.raw_payload->'receipt_date_correction')
                    ELSE EXCLUDED.raw_payload END
            WHERE NOT (COALESCE(expenses.raw_payload, '{{}}'::jsonb) ? 'item_allocation_correction')
              AND NOT (COALESCE(expenses.raw_payload, '{{}}'::jsonb) ? 'duplicate_merge')
            RETURNING id
            """,
            (receipt.canonical_order_id, Path(receipt.path).name, receipt.source_reference,
             receipt.source_sha256, receipt.transaction_date, receipt.merchant, receipt.subtotal,
             gst, pst, receipt.total, receipt.total, receipt.extraction_status,
             receipt.extraction_confidence, receipt.payment_method, receipt.card_last4,
             receipt.payer, json.dumps(payload)),
        )
        saved = cur.fetchone()
        if saved is None:
            # Human/image-verified canonical allocations survive later OCR.
            cur.execute(f"SELECT COALESCE((raw_payload->'duplicate_merge'->>'canonical_expense_pk')::bigint,id) FROM {schema}.expenses WHERE source='scanned' AND order_id=%s",
                        (receipt.canonical_order_id,))
            return int(cur.fetchone()[0])
        expense_pk = int(saved[0])
        # Re-read under the upsert lock: another delivery may have inserted the
        # purchase after our initial lookup. Reuse those rows instead of duplicating.
        _, existing_items = load_existing(cur, receipt.canonical_order_id, schema)
        with telemetry.span("receipt.categorization"):
            persist_items(cur, receipt, expense_pk, existing_items, schema)
    return expense_pk


def receipt_to_dict(receipt: ScannedReceipt) -> dict:
    data = asdict(receipt)
    data["order_id"] = receipt.canonical_order_id
    return data


def print_review(receipts: Sequence[ScannedReceipt]) -> None:
    flagged = [r for r in receipts if r.extraction_status != "complete"]
    if not flagged:
        print("No receipts require review.")
        return
    print("\nReceipts requiring review:")
    for r in flagged:
        merchant = r.merchant or "-"
        date = r.transaction_date or "-"
        total = f"{r.total:.2f}" if r.total is not None else "-"
        reasons = ",".join(r.review_reasons) or "-"
        payment = r.payment_method or "-"
        card = r.card_last4 or "-"
        payer = r.payer or "-"
        print(
            f"{r.source_reference}  status={r.extraction_status} "
            f"confidence={r.extraction_confidence:.2f} merchant={merchant!r} "
            f"date={date} total={total} items={len(r.items)} "
            f"payment={payment} card={card} payer={payer} reasons={reasons}"
        )


def main() -> int:
    args = parse_args()
    root = Path(args.input_path).expanduser().resolve()
    paths = discover_scans(root)
    receipts = [parse_scan(path, root) for path in paths]

    if args.write_db:
        conn = _db_connect(args.db_dsn)
        try:
            for receipt in receipts:
                upsert_receipt(conn, receipt, args.db_schema)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    report = {
        "discovered": len(paths),
        "complete": sum(r.extraction_status == "complete" for r in receipts),
        "review": sum(r.extraction_status == "review" for r in receipts),
        "unreadable": sum(r.extraction_status == "unreadable" for r in receipts),
        "receipts": [receipt_to_dict(r) for r in receipts],
    }
    if args.json_path:
        Path(args.json_path).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "receipts"}, indent=2))
    if args.show_review:
        print_review(receipts)
    return 0 if paths else 2


if __name__ == "__main__":
    raise SystemExit(main())
