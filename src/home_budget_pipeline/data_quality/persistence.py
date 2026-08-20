"""PostgreSQL persistence for KAN-70 data-quality results."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Sequence

from .required_fields import (
    CanonicalExpense,
    CanonicalExpenseItem,
    DataQualityResult,
    DataQualityViolation,
)


@dataclass
class PostgresDataQualityStore:
    connection: object

    def load_expense(self, expense_pk: int) -> CanonicalExpense:
        sql = """
            SELECT id, source, order_id, order_date, transaction_datetime,
                   store_name, expense_total
            FROM budget.expenses
            WHERE id = %s
        """
        with self.connection.cursor() as cur:
            cur.execute(sql, (expense_pk,))
            row = cur.fetchone()
        if row is None:
            raise LookupError(f"canonical expense not found: {expense_pk}")
        row_expense_pk, source, order_id, order_date, transaction_datetime, store_name, expense_total = row
        return CanonicalExpense(
            expense_pk=int(row_expense_pk),
            source=str(source),
            order_id=order_id,
            order_date=order_date if isinstance(order_date, date) else None,
            transaction_datetime=(
                transaction_datetime if isinstance(transaction_datetime, datetime) else None
            ),
            store_name=store_name,
            expense_total=(Decimal(expense_total) if expense_total is not None else None),
        )

    def load_items(self, expense_pk: int) -> Sequence[CanonicalExpenseItem]:
        sql = """
            SELECT id, expense_pk, item_name, line_total
            FROM budget.expense_items
            WHERE expense_pk = %s
            ORDER BY id
        """
        with self.connection.cursor() as cur:
            cur.execute(sql, (expense_pk,))
            rows = cur.fetchall()
        return [
            CanonicalExpenseItem(
                id=int(item_id),
                expense_pk=int(row_expense_pk),
                item_name=item_name,
                line_total=(Decimal(line_total) if line_total is not None else None),
            )
            for item_id, row_expense_pk, item_name, line_total in rows
        ]

    def save_result(self, result: DataQualityResult) -> None:
        summary_sql = """
            INSERT INTO budget.expense_data_quality_results (
                expense_pk, status, requires_review, validated_at
            )
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (expense_pk) DO UPDATE SET
                status = EXCLUDED.status,
                requires_review = EXCLUDED.requires_review,
                validated_at = NOW()
        """
        delete_sql = "DELETE FROM budget.expense_data_quality_violations WHERE expense_pk = %s"
        insert_violation_sql = """
            INSERT INTO budget.expense_data_quality_violations (
                expense_pk, rule, field_name, item_id, message
            )
            VALUES (%s, %s, %s, %s, %s)
        """
        with self.connection.cursor() as cur:
            cur.execute(
                summary_sql,
                (result.expense_pk, result.status.value, result.requires_review),
            )
            cur.execute(delete_sql, (result.expense_pk,))
            for violation in result.violations:
                cur.execute(
                    insert_violation_sql,
                    (
                        result.expense_pk,
                        violation.rule,
                        violation.field,
                        violation.item_id,
                        violation.message,
                    ),
                )
        self.connection.commit()


def violation_values(violation: DataQualityViolation) -> tuple[object, ...]:
    return (
        violation.rule,
        violation.field,
        violation.item_id,
        violation.message,
    )
