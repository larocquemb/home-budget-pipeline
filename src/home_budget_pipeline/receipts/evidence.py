"""Receipt evidence persistence and conservative transaction reconciliation.

A source document is immutable evidence. Multiple evidence rows (paper scan,
electronic receipt, email, photo) may point at one canonical budget.expenses row.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .annotations import extract_receipt_annotations


@dataclass(frozen=True)
class MatchResult:
    expense_pk: Optional[int]
    score: float
    disposition: str
    reason: str


def _safe_schema(schema: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        raise ValueError("invalid database schema")
    return schema


def _norm(value: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def resolve_merchant_alias(conn, merchant: Optional[str], schema: str = "budget") -> Optional[str]:
    """Resolve an OCR merchant candidate using database-managed aliases."""
    schema = _safe_schema(schema)
    normalized = _norm(merchant)
    if not normalized:
        return merchant
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT alias_name, merchant_name FROM {schema}.merchant_aliases WHERE is_active ORDER BY length(alias_name) DESC"
        )
        for alias_name, merchant_name in cur.fetchall():
            alias = _norm(alias_name)
            if alias and re.search(rf"(?:^|\s){re.escape(alias)}(?:\s|$)", normalized):
                return str(merchant_name)
    return merchant


def _dt(value: str | datetime | None) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def candidate_score(receipt, candidate: dict) -> float:
    score = 0.0
    receipt_id = (getattr(receipt, "receipt_id", None) or "").strip().lower()
    candidate_order_id = str(candidate.get("order_id") or "").strip().lower()
    candidate_receipt_id = str(candidate.get("receipt_id") or "").strip().lower()
    if receipt_id and receipt_id in {candidate_order_id, candidate_receipt_id}:
        score += 0.30
    total = getattr(receipt, "total", None)
    candidate_total = candidate.get("total")
    if total is not None and candidate_total is not None and abs(float(total) - float(candidate_total)) <= 0.01:
        score += 0.25
    merchant = _norm(getattr(receipt, "merchant", None))
    candidate_merchant = _norm(candidate.get("merchant"))
    if merchant and candidate_merchant and merchant == candidate_merchant:
        score += 0.15
    card = (getattr(receipt, "card_last4", None) or "").strip()
    candidate_card = str(candidate.get("card_last4") or "").strip()
    if card and candidate_card and card == candidate_card:
        score += 0.10
    receipt_dt = _dt(getattr(receipt, "transaction_datetime", None))
    candidate_dt = _dt(candidate.get("transaction_datetime"))
    if receipt_dt and candidate_dt:
        seconds = abs((receipt_dt - candidate_dt).total_seconds())
        if seconds <= 120:
            score += 0.20
        elif receipt_dt.date() == candidate_dt.date():
            score += 0.05
    else:
        receipt_date = getattr(receipt, "transaction_date", None)
        candidate_date = candidate.get("order_date")
        if receipt_date and candidate_date and str(receipt_date) == str(candidate_date):
            score += 0.05
    return round(score, 4)


def find_match(conn, receipt, schema: str = "budget") -> MatchResult:
    schema = _safe_schema(schema)
    transaction_date = getattr(receipt, "transaction_date", None)
    total = getattr(receipt, "total", None)
    if not transaction_date and total is None:
        return MatchResult(None, 0.0, "new", "insufficient_match_keys")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, order_id, order_date, store_name,
                   receipt_total_charged, expense_total,
                   transaction_datetime, card_last4,
                   raw_payload ->> 'receipt_id' AS receipt_id
              FROM {schema}.expenses
             WHERE (%s::date IS NULL OR order_date = %s::date OR transaction_datetime::date = %s::date)
               AND (%s::numeric IS NULL OR ABS(COALESCE(receipt_total_charged, expense_total) - %s::numeric) <= 0.01)
             ORDER BY id
            """,
            (transaction_date, transaction_date, transaction_date, total, total),
        )
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description]
    scored = []
    for row in rows:
        candidate = dict(zip(columns, row))
        candidate["merchant"] = candidate.pop("store_name", None)
        candidate["total"] = candidate.get("receipt_total_charged") or candidate.get("expense_total")
        scored.append((candidate_score(receipt, candidate), int(candidate["id"])))
    scored.sort(reverse=True)
    if not scored:
        return MatchResult(None, 0.0, "new", "no_candidate")
    best_score, best_id = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else -1.0
    if best_score >= 0.80 and best_score - second_score >= 0.10:
        return MatchResult(best_id, best_score, "matched", "high_confidence_match")
    if best_score >= 0.45:
        return MatchResult(None, best_score, "ambiguous", "plausible_existing_transaction")
    return MatchResult(None, best_score, "new", "no_plausible_match")


def persist_auto_annotations(conn, evidence_id: int, text: str, schema: str = "budget") -> int:
    schema = _safe_schema(schema)
    annotations = extract_receipt_annotations(text)
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {schema}.receipt_annotations WHERE evidence_id = %s AND raw_payload ->> 'source' = 'auto_ocr'", (evidence_id,))
        for annotation in annotations:
            cur.execute(
                f"""
                INSERT INTO {schema}.receipt_annotations (
                    evidence_id, annotation_type, text, normalized_category,
                    amount, line_index, confidence, raw_payload
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (evidence_id, annotation.annotation_type, annotation.text, annotation.normalized_category,
                 annotation.amount, annotation.line_index, annotation.confidence, json.dumps({"source": "auto_ocr"})),
            )
        cur.execute(
            f"""
            UPDATE {schema}.receipt_evidence
               SET has_handwritten_notes = %s,
                   has_category_markup = %s,
                   updated_at = NOW()
             WHERE id = %s
            """,
            (any(a.annotation_type == "note" for a in annotations),
             any(a.annotation_type in {"category_subtotal", "category_label", "separator"} for a in annotations),
             evidence_id),
        )
    return len(annotations)


def upsert_evidence(conn, receipt, schema: str = "budget", evidence_type: str = "scanned", expense_pk: Optional[int] = None) -> int:
    schema = _safe_schema(schema)
    payload = {"receipt_id": getattr(receipt, "receipt_id", None), "review_reasons": getattr(receipt, "review_reasons", []), "payer": getattr(receipt, "payer", None)}
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {schema}.receipt_evidence (
                expense_pk, evidence_type, source_reference, source_sha256,
                transaction_datetime, merchant, receipt_id, total,
                payment_method, card_last4, extraction_status,
                extraction_confidence, raw_text, page_text, raw_payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_sha256) DO UPDATE SET
                expense_pk = COALESCE(EXCLUDED.expense_pk, {schema}.receipt_evidence.expense_pk),
                transaction_datetime = EXCLUDED.transaction_datetime,
                merchant = EXCLUDED.merchant,
                receipt_id = EXCLUDED.receipt_id,
                total = EXCLUDED.total,
                payment_method = EXCLUDED.payment_method,
                card_last4 = EXCLUDED.card_last4,
                extraction_status = EXCLUDED.extraction_status,
                extraction_confidence = EXCLUDED.extraction_confidence,
                raw_text = EXCLUDED.raw_text,
                page_text = EXCLUDED.page_text,
                raw_payload = EXCLUDED.raw_payload,
                updated_at = NOW()
            RETURNING id
            """,
            (expense_pk, evidence_type, receipt.source_reference, receipt.source_sha256,
             getattr(receipt, "transaction_datetime", None), receipt.merchant, receipt.receipt_id,
             receipt.total, receipt.payment_method, receipt.card_last4,
             receipt.extraction_status, receipt.extraction_confidence, receipt.text,
             json.dumps(receipt.page_text), json.dumps(payload)),
        )
        evidence_id = int(cur.fetchone()[0])
    persist_ocr_lines(conn, evidence_id, getattr(receipt, "ocr_layout", []), schema)
    persist_ocr_run(conn, evidence_id, receipt, schema)
    persist_auto_annotations(conn, evidence_id, receipt.text, schema)
    return evidence_id


def persist_ocr_run(conn, evidence_id: int, receipt, schema: str = "budget") -> int:
    """Persist one immutable OCR run and its pass-level telemetry."""
    schema = _safe_schema(schema)
    run = getattr(receipt, "ocr_run", None) or {}
    run_uuid = run.get("run_uuid")
    passes = run.get("ocr_passes")
    if not run_uuid or not isinstance(passes, list):
        return 0
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {schema}.receipt_ocr_runs (
                run_uuid, evidence_id, source_sha256, source_reference, cache_version,
                processed_at, processing_seconds, worker_host, worker_pid,
                merchant, extraction_status, extraction_confidence, timings
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_uuid) DO NOTHING
            """,
            (
                run_uuid, evidence_id, receipt.source_sha256,
                getattr(receipt, "source_reference", ""),
                run.get("cache_version"),
                run.get("processed_at"), run.get("processing_seconds"),
                getattr(receipt, "worker_host", None), getattr(receipt, "worker_pid", None),
                receipt.merchant, receipt.extraction_status, receipt.extraction_confidence,
                json.dumps(run.get("timings") or {}),
            ),
        )
        for metric in passes:
            cur.execute(
                f"""
                INSERT INTO {schema}.receipt_ocr_passes (
                    run_uuid, pass_id, page_number, engine, engine_type, dpi, psm,
                    variant, seconds, status, error_type, line_count, character_count,
                    structural_score, summary_score, valid_timestamp, selected_base,
                    consensus_line_coverage, consensus_coverage_ratio, extracted_text,
                    quality, engine_options, usage, provenance
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                          %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (run_uuid, pass_id) DO NOTHING
                """,
                (
                    run_uuid, metric.get("pass_id"), metric.get("page_number"),
                    metric.get("engine"), metric.get("engine_type"), metric.get("dpi"),
                    metric.get("psm"), metric.get("variant"), metric.get("seconds"),
                    metric.get("status"), metric.get("error_type"), metric.get("line_count"),
                    metric.get("character_count"), metric.get("structural_score"),
                    metric.get("summary_score"), metric.get("valid_timestamp"),
                    bool(metric.get("selected_base")), metric.get("consensus_line_coverage"),
                    metric.get("consensus_coverage_ratio"), metric.get("text"),
                    json.dumps(metric.get("quality") or {}),
                    json.dumps(metric.get("engine_options") or {}),
                    json.dumps(metric.get("usage") or {}),
                    json.dumps(metric.get("provenance") or {}),
                ),
            )
    return len(passes)


def record_ocr_feedback(
    conn,
    evidence_id: int,
    outcome: str,
    corrected_fields: Optional[dict] = None,
    notes: Optional[str] = None,
    verified_by: Optional[str] = None,
    schema: str = "budget",
) -> None:
    """Record verified ground truth for evaluating OCR configurations."""
    schema = _safe_schema(schema)
    if outcome not in {"confirmed", "corrected", "rejected"}:
        raise ValueError("outcome must be confirmed, corrected, or rejected")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {schema}.receipt_ocr_feedback (
                evidence_id, outcome, corrected_fields, notes, verified_by
            ) VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (evidence_id) DO UPDATE SET
                outcome = EXCLUDED.outcome,
                corrected_fields = EXCLUDED.corrected_fields,
                notes = EXCLUDED.notes,
                verified_by = EXCLUDED.verified_by,
                verified_at = NOW(),
                updated_at = NOW()
            """,
            (evidence_id, outcome, json.dumps(corrected_fields or {}), notes, verified_by),
        )


def persist_ocr_lines(conn, evidence_id: int, pages: list[dict], schema: str = "budget") -> int:
    """Replace structured OCR lines belonging to one receipt evidence record."""
    schema = _safe_schema(schema)
    inserted = 0
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {schema}.receipt_ocr_lines WHERE evidence_id = %s", (evidence_id,))
        def walk(lines, parent=None):
            for line in lines:
                yield line, parent
                yield from walk(line.get("children", []), line)

        for page in pages:
            page_number = page.get("page_number")
            for line, parent in walk(page.get("lines", [])):
                layout = line.get("layout") or {}
                bounds = layout.get("bounds") or line.get("bounds") or {}
                page_layout = layout.get("page") or {}
                indent = layout.get("indent") or {}
                applies_to = line.get("applies_to") or {}
                if parent and line.get("line_type") in {"discount", "points", "price_detail", "fee"}:
                    applies_to = {"page_number": page_number, "line_number": parent.get("line_number")}
                confidence = line.get("confidence")
                cur.execute(
                    f"""
                    INSERT INTO {schema}.receipt_ocr_lines (
                        evidence_id, page_number, line_number, text, confidence,
                        x, y, width, height, page_left, page_width,
                        indent_pixels, indent_ratio, indent_level,
                        line_type, department, applies_to_page, applies_to_line
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        evidence_id, page_number, line.get("line_number"), line.get("text"),
                        confidence / 100 if confidence is not None else None,
                        bounds.get("x"), bounds.get("y"), bounds.get("width"), bounds.get("height"),
                        page_layout.get("left"), page_layout.get("width"), indent.get("pixels"),
                        indent.get("ratio"), indent.get("level"), line.get("line_type"),
                        line.get("department"), applies_to.get("page_number"), applies_to.get("line_number"),
                    ),
                )
                inserted += 1
    return inserted


def attach_evidence(conn, evidence_id: int, expense_pk: int, schema: str = "budget") -> None:
    schema = _safe_schema(schema)
    with conn.cursor() as cur:
        cur.execute(f"UPDATE {schema}.receipt_evidence SET expense_pk = %s, updated_at = NOW() WHERE id = %s", (expense_pk, evidence_id))
