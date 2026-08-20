"""Canonical expense categorization orchestration for KAN-68.

This module deliberately operates on ``expense_pk`` (the canonical purchase key), not
payment accounts or source receipts. KAN-77 can later link duplicate receipt evidence
to the same canonical expense without changing the categorization contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import List, Optional, Protocol, Sequence

from .pipeline import CategoryDecision, CategoryMappingPipeline


@dataclass(frozen=True)
class CanonicalExpenseItem:
    id: int
    expense_pk: int
    item_name: str
    line_total: Optional[Decimal]


@dataclass(frozen=True)
class ExpenseCategorySplit:
    expense_pk: int
    category_name: str
    category_amount: Decimal
    item_count: int
    requires_review: bool


class ExpenseCategorizationStore(Protocol):
    """Persistence boundary for canonical expense categorization."""

    def load_items(self, expense_pk: int) -> Sequence[CanonicalExpenseItem]: ...

    def save_decision(self, item_id: int, decision: CategoryDecision) -> None: ...

    def load_splits(self, expense_pk: int) -> Sequence[ExpenseCategorySplit]: ...

    def commit(self) -> None: ...


@dataclass
class CategorizedExpense:
    expense_pk: int
    decisions: List[tuple[CanonicalExpenseItem, CategoryDecision]]
    splits: Sequence[ExpenseCategorySplit]

    @property
    def requires_review(self) -> bool:
        return any(decision.requires_review for _, decision in self.decisions)


class CanonicalExpenseCategorizer:
    """Categorize every line item belonging to one canonical expense exactly once."""

    def __init__(
        self,
        pipeline: CategoryMappingPipeline,
        store: ExpenseCategorizationStore,
    ) -> None:
        self.pipeline = pipeline
        self.store = store

    def categorize_expense(
        self,
        expense_pk: int,
        *,
        source: Optional[str] = None,
        merchant: Optional[str] = None,
    ) -> CategorizedExpense:
        if expense_pk <= 0:
            raise ValueError("expense_pk must be positive")

        items = list(self.store.load_items(expense_pk))
        decisions: List[tuple[CanonicalExpenseItem, CategoryDecision]] = []

        for item in items:
            decision = self.pipeline.categorize(
                item.item_name,
                source=source,
                merchant=merchant,
            )
            self.store.save_decision(item.id, decision)
            decisions.append((item, decision))

        self.store.commit()
        splits = self.store.load_splits(expense_pk)
        return CategorizedExpense(
            expense_pk=expense_pk,
            decisions=decisions,
            splits=splits,
        )


@dataclass
class PostgresExpenseCategorizationStore:
    """PostgreSQL adapter for canonical expense categorization decisions."""

    connection: object

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
                item_name=str(item_name),
                line_total=line_total,
            )
            for item_id, row_expense_pk, item_name, line_total in rows
        ]

    def save_decision(self, item_id: int, decision: CategoryDecision) -> None:
        sql = """
            UPDATE budget.expense_items
            SET
                budget_category = %s,
                category_source = %s,
                category_confidence = %s,
                category_rationale = %s,
                category_requires_review = %s,
                categorized_at = NOW()
            WHERE id = %s
        """
        with self.connection.cursor() as cur:
            cur.execute(
                sql,
                (
                    decision.category,
                    decision.provenance.value,
                    decision.confidence,
                    decision.rationale,
                    decision.requires_review,
                    item_id,
                ),
            )
            if cur.rowcount != 1:
                raise LookupError(f"expense item not found: {item_id}")

    def load_splits(self, expense_pk: int) -> Sequence[ExpenseCategorySplit]:
        sql = """
            SELECT expense_pk, category_name, category_amount, item_count, requires_review
            FROM budget.expense_category_splits
            WHERE expense_pk = %s
            ORDER BY category_name
        """
        with self.connection.cursor() as cur:
            cur.execute(sql, (expense_pk,))
            rows = cur.fetchall()
        return [
            ExpenseCategorySplit(
                expense_pk=int(row_expense_pk),
                category_name=str(category_name),
                category_amount=Decimal(category_amount),
                item_count=int(item_count),
                requires_review=bool(requires_review),
            )
            for (
                row_expense_pk,
                category_name,
                category_amount,
                item_count,
                requires_review,
            ) in rows
        ]

    def commit(self) -> None:
        self.connection.commit()
