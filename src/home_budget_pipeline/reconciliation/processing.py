"""Canonical purchase reconciliation for KAN-69.

The reconciliation layer operates on canonical expense data after ingestion and
before downstream analytics/export. It is intentionally deterministic and does
not mutate source monetary values.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Optional, Sequence


ZERO = Decimal("0")
DEFAULT_TOLERANCE = Decimal("0.01")


class ReconciliationStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    NOT_CHECKABLE = "not_checkable"


@dataclass(frozen=True)
class CanonicalPurchaseAmounts:
    """Monetary components required for canonical purchase reconciliation."""

    expense_pk: int
    subtotal: Optional[Decimal]
    total: Optional[Decimal]
    taxes: Optional[Decimal] = None
    fees: Optional[Decimal] = None
    tip: Optional[Decimal] = None
    discounts: Optional[Decimal] = None


@dataclass(frozen=True)
class CanonicalPurchaseItem:
    id: int
    expense_pk: int
    line_total: Optional[Decimal]
    category_name: Optional[str] = None


@dataclass(frozen=True)
class CategorySplit:
    expense_pk: int
    category_name: str
    amount: Decimal


@dataclass(frozen=True)
class ReconciliationCheck:
    status: ReconciliationStatus
    variance: Optional[Decimal]
    expected: Optional[Decimal]
    actual: Optional[Decimal]
    tolerance: Decimal
    reason: Optional[str] = None

    @property
    def requires_review(self) -> bool:
        return self.status is ReconciliationStatus.FAIL


@dataclass(frozen=True)
class PurchaseReconciliationResult:
    expense_pk: int
    item_subtotal: ReconciliationCheck
    receipt_total: ReconciliationCheck
    category_splits: ReconciliationCheck

    @property
    def requires_review(self) -> bool:
        return any(
            check.requires_review
            for check in (self.item_subtotal, self.receipt_total, self.category_splits)
        )


class CanonicalPurchaseReconciler:
    """Run deterministic reconciliation checks for one canonical expense."""

    def __init__(self, *, tolerance: Decimal = DEFAULT_TOLERANCE) -> None:
        if tolerance < ZERO:
            raise ValueError("tolerance must be non-negative")
        self.tolerance = tolerance

    def reconcile(
        self,
        purchase: CanonicalPurchaseAmounts,
        items: Sequence[CanonicalPurchaseItem],
        category_splits: Sequence[CategorySplit],
    ) -> PurchaseReconciliationResult:
        if purchase.expense_pk <= 0:
            raise ValueError("expense_pk must be positive")
        self._validate_keys(purchase.expense_pk, items, category_splits)

        return PurchaseReconciliationResult(
            expense_pk=purchase.expense_pk,
            item_subtotal=self._reconcile_items_to_subtotal(purchase, items),
            receipt_total=self._reconcile_receipt_equation(purchase),
            category_splits=self._reconcile_category_splits(items, category_splits),
        )

    def _reconcile_items_to_subtotal(
        self,
        purchase: CanonicalPurchaseAmounts,
        items: Sequence[CanonicalPurchaseItem],
    ) -> ReconciliationCheck:
        if purchase.subtotal is None:
            return self._not_checkable("receipt subtotal is missing")
        if any(item.line_total is None for item in items):
            return self._not_checkable("one or more item line totals are missing")

        item_total = sum((item.line_total or ZERO for item in items), ZERO)
        return self._compare(expected=purchase.subtotal, actual=item_total)

    def _reconcile_receipt_equation(
        self,
        purchase: CanonicalPurchaseAmounts,
    ) -> ReconciliationCheck:
        if purchase.subtotal is None:
            return self._not_checkable("receipt subtotal is missing")
        if purchase.total is None:
            return self._not_checkable("receipt total is missing")

        # Optional components are treated as zero only when absent from the canonical
        # purchase model. Sources that cannot determine whether a component exists
        # should leave the complete equation uncheckable before constructing this model.
        expected_total = (
            purchase.subtotal
            + (purchase.taxes or ZERO)
            + (purchase.fees or ZERO)
            + (purchase.tip or ZERO)
            - (purchase.discounts or ZERO)
        )
        return self._compare(expected=expected_total, actual=purchase.total)

    def _reconcile_category_splits(
        self,
        items: Sequence[CanonicalPurchaseItem],
        category_splits: Sequence[CategorySplit],
    ) -> ReconciliationCheck:
        categorized_items = [item for item in items if item.category_name is not None]
        if any(item.line_total is None for item in categorized_items):
            return self._not_checkable("one or more categorized item totals are missing")

        categorized_total = sum((item.line_total or ZERO for item in categorized_items), ZERO)
        split_total = sum((split.amount for split in category_splits), ZERO)
        return self._compare(expected=categorized_total, actual=split_total)

    def _compare(self, *, expected: Decimal, actual: Decimal) -> ReconciliationCheck:
        variance = actual - expected
        status = (
            ReconciliationStatus.PASS
            if abs(variance) <= self.tolerance
            else ReconciliationStatus.FAIL
        )
        return ReconciliationCheck(
            status=status,
            variance=variance,
            expected=expected,
            actual=actual,
            tolerance=self.tolerance,
        )

    def _not_checkable(self, reason: str) -> ReconciliationCheck:
        return ReconciliationCheck(
            status=ReconciliationStatus.NOT_CHECKABLE,
            variance=None,
            expected=None,
            actual=None,
            tolerance=self.tolerance,
            reason=reason,
        )

    @staticmethod
    def _validate_keys(
        expense_pk: int,
        items: Sequence[CanonicalPurchaseItem],
        category_splits: Sequence[CategorySplit],
    ) -> None:
        if any(item.expense_pk != expense_pk for item in items):
            raise ValueError("all items must belong to the canonical expense")
        if any(split.expense_pk != expense_pk for split in category_splits):
            raise ValueError("all category splits must belong to the canonical expense")
