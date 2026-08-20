"""Deterministic required-field validation for canonical expenses (KAN-70)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Optional, Sequence


class DataQualityStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class CanonicalExpense:
    expense_pk: int
    source: str
    order_id: Optional[str]
    order_date: Optional[date] = None
    transaction_datetime: Optional[datetime] = None
    store_name: Optional[str] = None
    expense_total: Optional[Decimal] = None


@dataclass(frozen=True)
class CanonicalExpenseItem:
    id: int
    expense_pk: int
    item_name: Optional[str]
    line_total: Optional[Decimal]


@dataclass(frozen=True)
class DataQualityViolation:
    rule: str
    field: str
    item_id: Optional[int] = None
    message: Optional[str] = None


@dataclass(frozen=True)
class DataQualityResult:
    expense_pk: int
    status: DataQualityStatus
    violations: tuple[DataQualityViolation, ...]

    @property
    def requires_review(self) -> bool:
        return self.status is DataQualityStatus.FAIL


class RequiredFieldValidator:
    """Validate canonical completeness without modifying or inferring values."""

    SUPPORTED_SOURCES = frozenset({"instacart", "costco", "sobeys", "scanned"})

    # Every canonical expense needs identity, a usable date, merchant attribution,
    # total value, and usable line items. Date is expressed as an either/or rule.
    def validate(
        self,
        expense: CanonicalExpense,
        items: Sequence[CanonicalExpenseItem],
    ) -> DataQualityResult:
        if expense.expense_pk <= 0:
            raise ValueError("expense_pk must be positive")
        if any(item.expense_pk != expense.expense_pk for item in items):
            raise ValueError("all items must belong to the canonical expense")

        violations: list[DataQualityViolation] = []
        self._require_text(violations, "missing_source", "source", expense.source)
        self._require_text(violations, "missing_order_id", "order_id", expense.order_id)
        self._require_text(violations, "missing_store_name", "store_name", expense.store_name)

        if expense.order_date is None and expense.transaction_datetime is None:
            violations.append(
                DataQualityViolation(
                    rule="missing_transaction_date",
                    field="order_date|transaction_datetime",
                    message="canonical expense requires an order date or transaction datetime",
                )
            )

        if expense.expense_total is None:
            violations.append(DataQualityViolation("missing_expense_total", "expense_total"))

        if not items:
            violations.append(DataQualityViolation("missing_items", "items"))

        for item in items:
            if item.id <= 0:
                violations.append(
                    DataQualityViolation("invalid_item_id", "id", item_id=item.id)
                )
            if item.item_name is None or not item.item_name.strip():
                violations.append(
                    DataQualityViolation("item_missing_name", "item_name", item_id=item.id)
                )
            if item.line_total is None:
                violations.append(
                    DataQualityViolation("item_missing_line_total", "line_total", item_id=item.id)
                )

        status = DataQualityStatus.FAIL if violations else DataQualityStatus.PASS
        return DataQualityResult(expense.expense_pk, status, tuple(violations))

    def source_rule_status(self, source: str, rule_source: str) -> DataQualityStatus:
        """Expose whether a source-specific rule applies without treating N/A as failure."""
        if source not in self.SUPPORTED_SOURCES:
            return DataQualityStatus.NOT_APPLICABLE
        return (
            DataQualityStatus.PASS
            if source == rule_source
            else DataQualityStatus.NOT_APPLICABLE
        )

    @staticmethod
    def _require_text(
        violations: list[DataQualityViolation],
        rule: str,
        field: str,
        value: Optional[str],
    ) -> None:
        if value is None or not value.strip():
            violations.append(DataQualityViolation(rule, field))
