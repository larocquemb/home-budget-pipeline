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


def _dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
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
    persist_auto_annotations(conn, evidence_id, receipt.text, schema)
    return evidence_id


def attach_evidence(conn, evidence_id: int, expense_pk: int, schema: str = "budget") -> None:
    schema = _safe_schema(schema)
    with conn.cursor() as cur:
        cur.execute(f"UPDATE {schema}.receipt_evidence SET expense_pk = %s, updated_at = NOW() WHERE id = %s", (expense_pk, evidence_id))
