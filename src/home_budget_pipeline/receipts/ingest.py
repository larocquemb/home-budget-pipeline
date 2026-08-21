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

OCR is deliberately local: Tesseract is invoked only when a PDF has no useful text layer.
The raw OCR text and page text are retained in raw_payload for later reprocessing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from payment_card_lookup import resolve_owner_from_db
from receipt_payment import extract_payment_provenance

SUPPORTED_EXTS = {".pdf", ".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff"}
MONEY_RE = re.compile(r"-?\$?\s*(\d{1,6}(?:,\d{3})*\.\d{2})-?")
OCR_MONEY_RE = re.compile(
    r"(?P<lead>-)?\$?\s*(?P<whole>\d{1,6})(?:\s*[,.:]\s*\.?\s*|\s+\.\s*)(?P<cents>\d{2})(?P<trail>-)?"
)
DATE_PATTERNS = (
    re.compile(r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b"),
    re.compile(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](20\d{2})\b"),
)
DATE_TIME_YYMMDD_RE = re.compile(r"\b(?:DATE\s*/?\s*TIME|DATE)\s*[:#-]?\s*(\d{2})/(\d{2})/(\d{2})\b", re.I)
MONTH_NAME_DATE_RE = re.compile(
    r"\b(\d{1,2})[- ](Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)[- ,]+(20\d{2})\b",
    re.I,
)
EMBEDDED_YYYYMMDD_RE = re.compile(r"(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])")
ID_PATTERNS = (
    re.compile(r"\b(?:receipt|transaction|trans|order|invoice)\s*(?:#|no\.?|number|id)?\s*[:#-]?\s*([A-Z0-9-]{4,})\b", re.I),
)
TOTAL_WORDS = re.compile(r"\b(total|subtotal|tax|gst|pst|hst|balance|tender|change|amount\s+due)\b", re.I)
NON_ITEM_WORDS = re.compile(r"\b(thank|visa|mastercard|debit|credit|approved|cashier|store|points?)\b", re.I)
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
    (re.compile(r"canad(?:ian|\s*tan|\w*)?\s+tire", re.I), "Canadian Tire"),
    (re.compile(r"(?:t|i|ti|im|tin|tim)\w*\s+hortons", re.I), "Tim Hortons"),
    (re.compile(r"wholesale", re.I), "Wholesale Club"),
    (re.compile(r"shoppers", re.I), "Shoppers Drug Mart"),
    (re.compile(r"best\s+buy", re.I), "Best Buy"),
    (re.compile(r"walmart", re.I), "Walmart"),
    (re.compile(r"sobeys", re.I), None),
)


@dataclass
class ScannedItem:
    item_name: str
    line_total: float


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
    text: str = ""
    extraction_confidence: float = 0.0
    extraction_status: str = "review"
    review_reasons: List[str] = field(default_factory=list)

    @property
    def canonical_order_id(self) -> str:
        return f"scan:{self.source_sha256}"


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


def _run_tesseract(path: Path, psm: str = "6") -> str:
    binary = shutil.which("tesseract")
    if not binary:
        return ""
    proc = subprocess.run([binary, str(path), "stdout", "--psm", psm], capture_output=True, text=True, check=False)
    return proc.stdout if proc.returncode == 0 else ""


def _ocr_pdf_pages(path: Path) -> List[str]:
    try:
        import pypdfium2 as pdfium  # type: ignore
    except Exception:
        return []
    pages: List[str] = []
    doc = pdfium.PdfDocument(str(path))
    try:
        for idx in range(len(doc)):
            image = doc[idx].render(scale=300 / 72).to_pil()
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp_path = Path(tmp.name)
            try:
                image.save(tmp_path)
                pages.append(_run_tesseract(tmp_path))
            finally:
                tmp_path.unlink(missing_ok=True)
    finally:
        doc.close()
    return pages


def extract_page_text(path: Path) -> List[str]:
    if path.suffix.lower() == ".pdf":
        try:
            import pdfplumber  # type: ignore
            with pdfplumber.open(str(path)) as pdf:
                pages = [(page.extract_text() or "") for page in pdf.pages]
            if any(p.strip() for p in pages):
                return pages
        except Exception:
            pass
        return _ocr_pdf_pages(path)
    return [_run_tesseract(path)]


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
        merged.extend(lines[overlap:])
    return "\n".join(merged)


def parse_money(line: str) -> Optional[float]:
    matches = list(MONEY_RE.finditer(line))
    if not matches:
        return None
    m = matches[-1]
    value = float(m.group(1).replace(",", ""))
    token = m.group(0).strip()
    return -value if token.startswith("-") or token.endswith("-") else value


def parse_money_tolerant(line: str) -> Optional[float]:
    """Parse OCR-damaged money only after the caller has anchored the line semantically."""
    matches = list(OCR_MONEY_RE.finditer(line))
    if not matches:
        return None
    m = matches[-1]
    value = float(f"{m.group('whole')}.{m.group('cents')}")
    return -value if m.group("lead") or m.group("trail") else value


def _valid_date(year: int, month: int, day: int) -> Optional[str]:
    try:
        return datetime(year, month, day).date().isoformat()
    except ValueError:
        return None


def extract_date(text: str) -> Optional[str]:
    for line in text.splitlines():
        m = DATE_TIME_YYMMDD_RE.search(line)
        if m:
            value = _valid_date(2000 + int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if value:
                return value

    month_map = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    for line in text.splitlines():
        m = MONTH_NAME_DATE_RE.search(line)
        if m:
            value = _valid_date(int(m.group(3)), month_map[m.group(2)[:3].lower()], int(m.group(1)))
            if value:
                return value

    for line in text.splitlines():
        for idx, pattern in enumerate(DATE_PATTERNS):
            m = pattern.search(line)
            if not m:
                continue
            if idx == 0:
                value = _valid_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            else:
                value = _valid_date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
            if value:
                return value

    for m in EMBEDDED_YYYYMMDD_RE.finditer(text):
        value = _valid_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if value:
            return value
    return None


def extract_receipt_id(text: str) -> Optional[str]:
    for line in text.splitlines():
        for pattern in ID_PATTERNS:
            m = pattern.search(line)
            if m:
                return m.group(1)
    return None


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


def extract_merchant(text: str) -> Optional[str]:
    for raw in text.splitlines()[:12]:
        line = normalize_line(raw)
        if not line or MONEY_RE.search(line) or len(line) > 70:
            continue
        if re.search(r"[A-Za-z]{3}", line) and not re.search(r"\b(receipt|invoice|date|tel|phone|transaction\s+record)\b", line, re.I):
            return normalize_merchant(line)
    return None


def extract_totals(text: str) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    subtotal = tax = total = None
    total_candidates: List[Tuple[float, bool]] = []
    tender_candidates: List[Tuple[float, bool]] = []
    for raw in text.splitlines():
        line = normalize_line(raw)
        total_like = bool(TOTAL_LIKE_RE.search(line))
        tender_like = bool(TENDER_LINE_RE.search(line)) and not re.search(
            r"\b(?:change|auth|reference|card\s+number)\b", line, re.I
        )
        amount = parse_money(line)
        tolerant = False
        if amount is None and (total_like or tender_like):
            amount = parse_money_tolerant(line)
            tolerant = amount is not None
        if amount is None:
            continue
        if re.search(r"\bsub\s*total\b", line, re.I):
            subtotal = amount
            continue
        if re.search(r"\b(?:total\s+tax|tax|gst|pst|hst)\b", line, re.I):
            tax = (tax or 0.0) + amount
            continue
        if total_like:
            if not re.search(r"\b(?:tax|items?|savings?|discount|points?)\b", line, re.I):
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
    return subtotal, tax, total


def extract_items(text: str) -> List[ScannedItem]:
    items: List[ScannedItem] = []
    for raw in text.splitlines():
        line = normalize_line(raw)
        if not line or TOTAL_WORDS.search(line) or NON_ITEM_WORDS.search(line):
            continue
        matches = list(MONEY_RE.finditer(line))
        if not matches:
            continue
        last = matches[-1]
        name = normalize_line(line[: last.start()].strip(" :-"))
        if len(re.sub(r"[^A-Za-z]", "", name)) < 2:
            continue
        amount = parse_money(line)
        if amount is not None:
            items.append(ScannedItem(name, amount))
    return items


def confidence_for(receipt: ScannedReceipt) -> float:
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
    if not receipt.merchant:
        reasons.append("missing_merchant")
    if not receipt.transaction_date:
        reasons.append("missing_date")
    if receipt.total is None:
        reasons.append("missing_total")
    if not receipt.items:
        reasons.append("missing_items")
    if receipt.extraction_confidence < 0.65:
        reasons.append("low_confidence")
    return reasons


def status_for(receipt: ScannedReceipt) -> str:
    reasons = review_reasons_for(receipt)
    if reasons == ["unreadable_ocr"]:
        return "unreadable"
    if reasons:
        return "review"
    return "complete"


def parse_scan(path: Path, source_root: Optional[Path] = None) -> ScannedReceipt:
    pages = extract_page_text(path)
    text = merge_page_text(pages)
    subtotal, tax, total = extract_totals(text)
    payment = extract_payment_provenance(text)
    try:
        reference = str(path.relative_to(source_root)) if source_root and source_root.is_dir() else path.name
    except ValueError:
        reference = path.name
    receipt = ScannedReceipt(
        path=str(path), source_reference=reference, source_sha256=sha256_file(path),
        merchant=extract_merchant(text), transaction_date=extract_date(text),
        receipt_id=extract_receipt_id(text), subtotal=subtotal, tax=tax, total=total,
        payment_method=payment.payment_method, card_last4=payment.card_last4,
        items=extract_items(text), page_text=list(pages), text=text,
    )
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
    receipt.payment_method = payment.payment_method
    receipt.card_last4 = payment.card_last4
    receipt.payer = resolve_owner_from_db(conn, payment, receipt.transaction_date, schema)

    payload = {
        "source_reference": receipt.source_reference,
        "source_sha256": receipt.source_sha256,
        "receipt_id": receipt.receipt_id,
        "payment_method": receipt.payment_method,
        "card_last4": receipt.card_last4,
        "payer": receipt.payer,
        "page_text": receipt.page_text,
        "ocr_text": receipt.text,
        "review_reasons": receipt.review_reasons,
    }
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {schema}.expenses (
                source, order_id, receipt_filename, source_reference, source_sha256,
                order_date, store_name, receipt_item_subtotal, receipt_gst,
                receipt_total_charged, expense_total, extraction_status,
                extraction_confidence, payment_method, card_last4, payer, raw_payload
            ) VALUES ('scanned', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source, order_id) DO UPDATE SET
                receipt_filename = EXCLUDED.receipt_filename,
                source_reference = EXCLUDED.source_reference,
                source_sha256 = EXCLUDED.source_sha256,
                order_date = EXCLUDED.order_date,
                store_name = EXCLUDED.store_name,
                receipt_item_subtotal = EXCLUDED.receipt_item_subtotal,
                receipt_gst = EXCLUDED.receipt_gst,
                receipt_total_charged = EXCLUDED.receipt_total_charged,
                expense_total = EXCLUDED.expense_total,
                extraction_status = EXCLUDED.extraction_status,
                extraction_confidence = EXCLUDED.extraction_confidence,
                payment_method = EXCLUDED.payment_method,
                card_last4 = EXCLUDED.card_last4,
                payer = EXCLUDED.payer,
                raw_payload = EXCLUDED.raw_payload
            RETURNING id
            """,
            (receipt.canonical_order_id, Path(receipt.path).name, receipt.source_reference,
             receipt.source_sha256, receipt.transaction_date, receipt.merchant, receipt.subtotal,
             receipt.tax or 0, receipt.total, receipt.total, receipt.extraction_status,
             receipt.extraction_confidence, receipt.payment_method, receipt.card_last4,
             receipt.payer, json.dumps(payload)),
        )
        expense_pk = int(cur.fetchone()[0])
        cur.execute(f"DELETE FROM {schema}.expense_items WHERE expense_pk = %s", (expense_pk,))
        for item in receipt.items:
            cur.execute(
                f"""INSERT INTO {schema}.expense_items (
                    expense_pk, item_name, unit_qty, unit_cost, line_total,
                    original_line_total, category_source
                ) VALUES (%s, %s, 1, %s, %s, %s, NULL)""",
                (expense_pk, item.item_name, item.line_total, item.line_total, item.line_total),
            )
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