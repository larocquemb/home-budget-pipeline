"""PostgreSQL query and narrowly scoped mutation service for the Ledger UI."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Optional

from home_budget_pipeline.categorization.logic import normalize_for_match


@dataclass(frozen=True)
class Page:
    rows: tuple[dict[str, Any], ...]
    limit: int
    offset: int
    total_count: int | None = None


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
        count_sql = f"""
            SELECT COUNT(*) AS total_count
              FROM budget.analytics_expenses
              {predicate}
        """
        count_rows = self._fetch(count_sql, tuple(params))
        total_count = int(count_rows[0]["total_count"])
        sql = f"""
            SELECT expense_pk, source, order_date, transaction_datetime, store_name,
                   account_name, expense_total, extraction_status, requires_review
              FROM budget.analytics_expenses
              {predicate}
             ORDER BY COALESCE(order_date, transaction_datetime::date) DESC NULLS LAST, expense_pk DESC
             LIMIT %s OFFSET %s
        """
        params.extend([limit, offset])
        rows = self._fetch(sql, tuple(params))
        return Page(rows, limit, offset, total_count)

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

    def extraction_audit(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        status: Optional[str] = None,
        max_items: int = 4,
    ) -> Page:
        where = "WHERE e.extraction_status = %s" if status else ""
        params: list[Any] = [status] if status else []
        sql = f"""
            SELECT e.id AS expense_pk,
                   (SELECT MIN(re.id) FROM budget.receipt_evidence re
                     WHERE re.expense_pk = e.id) AS evidence_id,
                   e.source,
                   e.source_reference, e.order_date, e.store_name,
                   e.expense_total, e.extraction_status,
                   COUNT(i.id) AS item_count
              FROM budget.expenses e
              LEFT JOIN budget.expense_items i ON i.expense_pk = e.id
              {where}
             GROUP BY e.id, e.source, e.source_reference, e.order_date,
                      e.store_name, e.expense_total, e.extraction_status
            HAVING COUNT(i.id) <= %s
             ORDER BY item_count, e.order_date DESC NULLS LAST, e.id DESC
             LIMIT %s OFFSET %s
        """
        params.extend([max_items, limit, offset])
        return Page(self._fetch(sql, tuple(params)), limit, offset)

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

    def active_categories(self) -> tuple[str, ...]:
        rows = self._fetch(
            """
            SELECT category_name
              FROM budget.expense_categories
             WHERE is_active = TRUE
             ORDER BY sort_order, category_name
            """
        )
        return tuple(str(row["category_name"]) for row in rows)

    def save_category_override(
        self,
        expense_item_id: int,
        category: str,
        *,
        actor_user: str,
        actor_email: Optional[str] = None,
    ) -> dict[str, Any]:
        """Create an approved exact rule and apply it to matching imported items."""
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT i.item_name, i.item_name_norm, e.source, e.store_name,
                           i.budget_category
                      FROM budget.expense_items i
                      JOIN budget.expenses e ON e.id = i.expense_pk
                     WHERE i.id = %s
                    """,
                    (expense_item_id,),
                )
                row = cur.fetchone()
                if row is None:
                    raise LookupError("expense item not found")
                if isinstance(row, dict):
                    item_name = row["item_name"]
                    item_name_norm = normalize_for_match(str(item_name))
                    source = row["source"]
                    merchant = row["store_name"]
                    old_category = row["budget_category"]
                else:
                    item_name, _, source, merchant, old_category = row
                    item_name_norm = normalize_for_match(str(item_name))

                cur.execute(
                    """
                    SELECT 1
                      FROM budget.expense_categories
                     WHERE category_name = %s AND is_active = TRUE
                    """,
                    (category,),
                )
                if cur.fetchone() is None:
                    raise ValueError("category is not active")

                cur.execute(
                    """
                    SELECT id, category_name
                      FROM budget.expense_category_mappings
                     WHERE COALESCE(lower(trim(source)), '') = COALESCE(lower(trim(%s)), '')
                       AND COALESCE(lower(trim(merchant)), '') = COALESCE(lower(trim(%s)), '')
                       AND match_type = 'exact'
                       AND lower(trim(match_text)) = lower(trim(%s))
                    """,
                    (source, merchant, item_name_norm),
                )
                existing_mapping = cur.fetchone()
                if existing_mapping is None:
                    action = "created"
                else:
                    action = "changed"

                cur.execute(
                    """
                    INSERT INTO budget.expense_category_mappings (
                        source, merchant, match_type, match_text, category_name,
                        priority, is_active, provenance, is_approved, notes
                    )
                    VALUES (%s, %s, 'exact', %s, %s, 10, TRUE, 'manual', TRUE,
                            'Created from Ledger expense item category override')
                    ON CONFLICT (
                        (COALESCE(lower(trim(source)), '')),
                        (COALESCE(lower(trim(merchant)), '')),
                        match_type,
                        (lower(trim(match_text)))
                    )
                    DO UPDATE SET
                        category_name = EXCLUDED.category_name,
                        priority = EXCLUDED.priority,
                        is_active = TRUE,
                        provenance = 'manual',
                        is_approved = TRUE,
                        notes = EXCLUDED.notes,
                        updated_at = NOW()
                    RETURNING id
                    """,
                    (source, merchant, item_name_norm, category),
                )
                mapping_row = cur.fetchone()
                mapping_id = mapping_row["id"] if isinstance(mapping_row, dict) else mapping_row[0]

                cur.execute(
                    """
                    UPDATE budget.expense_items i
                       SET budget_category = %s,
                           category_source = 'rule',
                           category_confidence = 1,
                           category_rationale = 'human-approved category override',
                           category_requires_review = FALSE,
                           categorized_at = NOW(),
                           updated_at = NOW()
                      FROM budget.expenses e
                     WHERE e.id = i.expense_pk
                       AND trim(lower(regexp_replace(i.item_name, '[^a-zA-Z0-9]+', ' ', 'g'))) = %s
                       AND e.source IS NOT DISTINCT FROM %s
                       AND lower(trim(e.store_name)) IS NOT DISTINCT FROM lower(trim(%s))
                    """,
                    (category, item_name_norm, source, merchant),
                )
                affected_items = cur.rowcount
                cur.execute(
                    """
                    INSERT INTO budget.expense_category_mapping_audit (
                        mapping_id, expense_item_id, actor_user, actor_email, action,
                        item_name, match_text, source, merchant, old_category,
                        new_category, affected_item_count
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id, created_at
                    """,
                    (
                        mapping_id, expense_item_id, actor_user, actor_email, action,
                        item_name, item_name_norm, source, merchant,
                        old_category, category, affected_items,
                    ),
                )
                audit_row = cur.fetchone()
                if isinstance(audit_row, dict):
                    audit_id, audited_at = audit_row["id"], audit_row["created_at"]
                else:
                    audit_id, audited_at = audit_row
            conn.commit()
            return {
                "mapping_id": mapping_id,
                "audit_id": audit_id,
                "audited_at": audited_at,
                "item_name": item_name,
                "category": category,
                "affected_items": affected_items,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

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
