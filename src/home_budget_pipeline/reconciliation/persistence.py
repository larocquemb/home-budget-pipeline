"""PostgreSQL persistence boundary for canonical reconciliation results."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from .processing import (
    CanonicalPurchaseAmounts,
    CanonicalPurchaseItem,
    CategorySplit,
    PurchaseReconciliationResult,
    ReconciliationCheck,
)


@dataclass
class PostgresReconciliationStore:
    """Load canonical expense data and persist deterministic reconciliation checks."""

    connection: object

    def load_purchase(self, expense_pk: int) -> CanonicalPurchaseAmounts:
        sql = """
            SELECT
                id,
                receipt_item_subtotal,
                receipt_total_charged,
                COALESCE(receipt_gst, 0) + COALESCE(receipt_pst, 0)
                    + COALESCE(receipt_service_fee_tax, 0) AS taxes,
                COALESCE(receipt_service_fee, 0) + COALESCE(receipt_recycling_fee, 0) AS fees,
                receipt_tip,
                receipt_discount_total,
                receipt_item_subtotal IS NOT NULL
                    AND receipt_total_charged IS NOT NULL AS receipt_equation_complete
            FROM budget.expenses
            WHERE id = %s
        """
        with self.connection.cursor() as cur:
            cur.execute(sql, (expense_pk,))
            row = cur.fetchone()
        if row is None:
            raise LookupError(f"canonical expense not found: {expense_pk}")

        (
            row_expense_pk,
            subtotal,
            total,
            taxes,
            fees,
            tip,
            discounts,
            receipt_equation_complete,
        ) = row
        return CanonicalPurchaseAmounts(
            expense_pk=int(row_expense_pk),
            subtotal=subtotal,
            total=total,
            taxes=taxes,
            fees=fees,
            tip=tip,
            discounts=discounts,
            receipt_equation_complete=bool(receipt_equation_complete),
        )

    def load_items(self, expense_pk: int) -> Sequence[CanonicalPurchaseItem]:
        sql = """
            SELECT id, expense_pk, line_total, budget_category
            FROM budget.expense_items
            WHERE expense_pk = %s
            ORDER BY id
        """
        with self.connection.cursor() as cur:
            cur.execute(sql, (expense_pk,))
            rows = cur.fetchall()
        return [
            CanonicalPurchaseItem(
                id=int(item_id),
                expense_pk=int(row_expense_pk),
                line_total=line_total,
                category_name=category_name,
            )
            for item_id, row_expense_pk, line_total, category_name in rows
        ]

    def load_category_splits(self, expense_pk: int) -> Sequence[CategorySplit]:
        sql = """
            SELECT expense_pk, category_name, category_amount
            FROM budget.expense_category_splits
            WHERE expense_pk = %s
            ORDER BY category_name
        """
        with self.connection.cursor() as cur:
            cur.execute(sql, (expense_pk,))
            rows = cur.fetchall()
        return [
            CategorySplit(
                expense_pk=int(row_expense_pk),
                category_name=str(category_name),
                amount=Decimal(category_amount),
            )
            for row_expense_pk, category_name, category_amount in rows
        ]

    def save_result(self, result: PurchaseReconciliationResult) -> None:
        sql = """
            INSERT INTO budget.expense_reconciliation_results (
                expense_pk,
                item_subtotal_status,
                item_subtotal_variance,
                receipt_total_status,
                receipt_total_variance,
                category_splits_status,
                category_splits_variance,
                requires_review,
                reconciled_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (expense_pk) DO UPDATE SET
                item_subtotal_status = EXCLUDED.item_subtotal_status,
                item_subtotal_variance = EXCLUDED.item_subtotal_variance,
                receipt_total_status = EXCLUDED.receipt_total_status,
                receipt_total_variance = EXCLUDED.receipt_total_variance,
                category_splits_status = EXCLUDED.category_splits_status,
                category_splits_variance = EXCLUDED.category_splits_variance,
                requires_review = EXCLUDED.requires_review,
                reconciled_at = NOW()
        """
        with self.connection.cursor() as cur:
            cur.execute(
                sql,
                (
                    result.expense_pk,
                    result.item_subtotal.status.value,
                    result.item_subtotal.variance,
                    result.receipt_total.status.value,
                    result.receipt_total.variance,
                    result.category_splits.status.value,
                    result.category_splits.variance,
                    result.requires_review,
                ),
            )
        self.connection.commit()


def check_values(check: ReconciliationCheck) -> tuple[object, ...]:
    """Stable tuple useful to downstream adapters and tests."""
    return (
        check.status.value,
        check.variance,
        check.expected,
        check.actual,
        check.tolerance,
        check.reason,
    )
