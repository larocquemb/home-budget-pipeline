"""Read-only PostgreSQL query service for the Ledger web UI."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass(frozen=True)
class Page:
    rows: tuple[dict[str, Any], ...]
    limit: int
    offset: int


class LedgerQueryService:
    """Server-side queries over canonical analytics views and review tables."""

    def __init__(self, connect: Optional[Callable[[], object]] = None):
        self._connect = connect or self._default_connect

    @staticmethod
    def _default_connect():
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)

    def _fetch(self, sql: str, params: tuple[Any, ...] = ()) -> tuple[dict[str, Any], ...]:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SET TRANSACTION READ ONLY")
                cur.execute(sql, params)
                rows = cur.fetchall()
                if rows and isinstance(rows[0], dict):
                    return tuple(rows)
                columns = [item[0] for item in cur.description]
                return tuple(dict(zip(columns, row)) for row in rows)
        finally:
            conn.close()

    def analytics_expenses(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        source: Optional[str] = None,
        merchant: Optional[str] = None,
        requires_review: Optional[bool] = None,
    ) -> Page:
        where: list[str] = []
        params: list[Any] = []
        if source:
            where.append("source = %s")
            params.append(source)
        if merchant:
            where.append("store_name ILIKE %s")
            params.append(f"%{merchant}%")
        if requires_review is not None:
            where.append("requires_review = %s")
            params.append(requires_review)
        predicate = " WHERE " + " AND ".join(where) if where else ""
        sql = f"""
            SELECT expense_pk, source, order_date, transaction_datetime, store_name,
                   account_name, expense_total, extraction_status, requires_review
              FROM budget.analytics_expenses
              {predicate}
             ORDER BY COALESCE(order_date, transaction_datetime::date) DESC NULLS LAST, expense_pk DESC
             LIMIT %s OFFSET %s
        """
        params.extend([limit, offset])
        return Page(self._fetch(sql, tuple(params)), limit, offset)

    def category_spend(self, *, limit: int = 100, offset: int = 0) -> Page:
        sql = """
            SELECT expense_date, budget_category, category_group_name,
                   category_amount, item_count, expense_count
              FROM budget.analytics_category_spend
             ORDER BY expense_date DESC NULLS LAST, category_amount DESC
             LIMIT %s OFFSET %s
        """
        return Page(self._fetch(sql, (limit, offset)), limit, offset)

    def review_queue(self, *, limit: int = 50, offset: int = 0) -> Page:
        sql = """
            SELECT expense_pk, source, order_date, transaction_datetime, store_name,
                   expense_total, extraction_status, data_quality_status,
                   data_quality_violation_count, requires_review
              FROM budget.analytics_review_queue
             ORDER BY COALESCE(order_date, transaction_datetime::date) DESC NULLS LAST, expense_pk DESC
             LIMIT %s OFFSET %s
        """
        return Page(self._fetch(sql, (limit, offset)), limit, offset)

    def expense_detail(self, expense_pk: int) -> dict[str, Any] | None:
        rows = self._fetch(
            """
            SELECT * FROM budget.analytics_expenses WHERE expense_pk = %s
            """,
            (expense_pk,),
        )
        if not rows:
            return None
        expense = dict(rows[0])
        expense["items"] = self._fetch(
            """
            SELECT expense_item_id, item_name, line_total, original_line_total,
                   budget_category, category_group_name, category_source,
                   category_confidence, category_requires_review
              FROM budget.analytics_expense_items
             WHERE expense_pk = %s
             ORDER BY expense_item_id
            """,
            (expense_pk,),
        )
        expense["evidence"] = self._fetch(
            """
            SELECT id, evidence_type, source_reference, transaction_datetime,
                   merchant, receipt_id, total, extraction_status,
                   extraction_confidence, card_last4, has_handwritten_notes,
                   has_category_markup
              FROM budget.receipt_evidence
             WHERE expense_pk = %s
             ORDER BY is_primary_source DESC, id
            """,
            (expense_pk,),
        )
        return expense

    def receipt_evidence(self, evidence_id: int) -> dict[str, Any] | None:
        rows = self._fetch(
            """
            SELECT id, expense_pk, evidence_type, source_reference, mime_type,
                   transaction_datetime, merchant, receipt_id, total,
                   payment_method, card_last4, extraction_status,
                   extraction_confidence, raw_text, page_text,
                   has_handwritten_notes, has_category_markup
              FROM budget.receipt_evidence
             WHERE id = %s
            """,
            (evidence_id,),
        )
        return dict(rows[0]) if rows else None

    def pending_duplicates(self, *, limit: int = 50, offset: int = 0) -> Page:
        sql = """
            SELECT id, left_evidence_id, right_evidence_id, canonical_expense_pk,
                   score, disposition, reasons, item_similarity, resolution_status
              FROM budget.receipt_duplicate_links
             WHERE resolution_status = 'pending'
             ORDER BY score DESC, id
             LIMIT %s OFFSET %s
        """
        return Page(self._fetch(sql, (limit, offset)), limit, offset)

    def transaction_reconciliation(self, *, limit: int = 50, offset: int = 0) -> Page:
        sql = """
            SELECT ft.id AS transaction_id, ft.transaction_date, ft.posting_date,
                   ft.amount, ft.description, ft.payer, ft.payer_source,
                   r.expense_pk, r.outcome, r.score, r.candidate_count,
                   r.candidate_details
              FROM budget.financial_transactions ft
              LEFT JOIN budget.transaction_expense_reconciliation r
                ON r.transaction_id = ft.id
             ORDER BY ft.transaction_date DESC, ft.id DESC
             LIMIT %s OFFSET %s
        """
        return Page(self._fetch(sql, (limit, offset)), limit, offset)

    def receipt_processing_status(self, *, limit: int = 100, offset: int = 0) -> Page:
        sql = """
            SELECT source_sha256, source_reference, status, attempts, last_error,
                   first_attempted_at, last_attempted_at, completed_at, updated_at
              FROM budget.receipt_processing_status
             ORDER BY COALESCE(last_attempted_at, updated_at) DESC, source_reference
             LIMIT %s OFFSET %s
        """
        return Page(self._fetch(sql, (limit, offset)), limit, offset)
