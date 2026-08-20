from decimal import Decimal

import pytest

from home_budget_pipeline.reconciliation.processing import (
    CanonicalPurchaseAmounts,
    CanonicalPurchaseItem,
    CanonicalPurchaseReconciler,
    CategorySplit,
    ReconciliationStatus,
)


def test_successful_reconciliation_passes_all_checks():
    purchase = CanonicalPurchaseAmounts(
        expense_pk=42,
        subtotal=Decimal("21.24"),
        taxes=Decimal("1.06"),
        fees=Decimal("2.00"),
        tip=Decimal("3.00"),
        discounts=Decimal("1.00"),
        total=Decimal("26.30"),
        receipt_equation_complete=True,
    )
    items = [
        CanonicalPurchaseItem(1, 42, Decimal("6.99"), "Groceries"),
        CanonicalPurchaseItem(2, 42, Decimal("10.00"), "Outdoor Supplies"),
        CanonicalPurchaseItem(3, 42, Decimal("4.25"), "Indoor Supplies"),
    ]
    splits = [
        CategorySplit(42, "Groceries", Decimal("6.99")),
        CategorySplit(42, "Outdoor Supplies", Decimal("10.00")),
        CategorySplit(42, "Indoor Supplies", Decimal("4.25")),
    ]

    result = CanonicalPurchaseReconciler().reconcile(purchase, items, splits)

    assert result.item_subtotal.status is ReconciliationStatus.PASS
    assert result.item_subtotal.variance == Decimal("0.00")
    assert result.receipt_total.status is ReconciliationStatus.PASS
    assert result.receipt_total.variance == Decimal("0.00")
    assert result.category_splits.status is ReconciliationStatus.PASS
    assert result.category_splits.variance == Decimal("0.00")
    assert result.requires_review is False


def test_rounding_variance_within_tolerance_passes():
    purchase = CanonicalPurchaseAmounts(
        expense_pk=42,
        subtotal=Decimal("10.00"),
        total=Decimal("10.00"),
        receipt_equation_complete=True,
    )
    items = [CanonicalPurchaseItem(1, 42, Decimal("10.01"), "Groceries")]
    splits = [CategorySplit(42, "Groceries", Decimal("10.01"))]

    result = CanonicalPurchaseReconciler(tolerance=Decimal("0.01")).reconcile(
        purchase, items, splits
    )

    assert result.item_subtotal.status is ReconciliationStatus.PASS
    assert result.item_subtotal.variance == Decimal("0.01")


def test_incomplete_receipt_equation_is_not_checkable_not_failure():
    purchase = CanonicalPurchaseAmounts(
        expense_pk=42,
        subtotal=Decimal("10.00"),
        total=Decimal("11.30"),
        taxes=Decimal("1.30"),
        receipt_equation_complete=False,
    )
    items = [CanonicalPurchaseItem(1, 42, Decimal("10.00"), "Groceries")]
    splits = [CategorySplit(42, "Groceries", Decimal("10.00"))]

    result = CanonicalPurchaseReconciler().reconcile(purchase, items, splits)

    assert result.receipt_total.status is ReconciliationStatus.NOT_CHECKABLE
    assert result.receipt_total.variance is None
    assert "complete receipt equation" in result.receipt_total.reason
    assert result.requires_review is False


def test_missing_item_amount_makes_item_check_not_checkable():
    purchase = CanonicalPurchaseAmounts(
        expense_pk=42,
        subtotal=Decimal("10.00"),
        total=Decimal("10.00"),
    )
    items = [CanonicalPurchaseItem(1, 42, None, None)]

    result = CanonicalPurchaseReconciler().reconcile(purchase, items, [])

    assert result.item_subtotal.status is ReconciliationStatus.NOT_CHECKABLE
    assert result.item_subtotal.variance is None


def test_failure_records_variance_and_requires_review_without_mutating_amounts():
    purchase = CanonicalPurchaseAmounts(
        expense_pk=42,
        subtotal=Decimal("10.00"),
        total=Decimal("11.30"),
        taxes=Decimal("1.30"),
        receipt_equation_complete=True,
    )
    items = [CanonicalPurchaseItem(1, 42, Decimal("9.50"), "Groceries")]
    splits = [CategorySplit(42, "Groceries", Decimal("9.00"))]

    result = CanonicalPurchaseReconciler().reconcile(purchase, items, splits)

    assert result.item_subtotal.status is ReconciliationStatus.FAIL
    assert result.item_subtotal.variance == Decimal("-0.50")
    assert result.item_subtotal.expected == Decimal("10.00")
    assert result.item_subtotal.actual == Decimal("9.50")
    assert result.category_splits.status is ReconciliationStatus.FAIL
    assert result.category_splits.variance == Decimal("-0.50")
    assert result.requires_review is True
    assert purchase.subtotal == Decimal("10.00")
    assert items[0].line_total == Decimal("9.50")


def test_category_split_check_counts_only_categorized_items():
    purchase = CanonicalPurchaseAmounts(
        expense_pk=42,
        subtotal=Decimal("15.00"),
        total=Decimal("15.00"),
    )
    items = [
        CanonicalPurchaseItem(1, 42, Decimal("10.00"), "Groceries"),
        CanonicalPurchaseItem(2, 42, Decimal("5.00"), None),
    ]
    splits = [CategorySplit(42, "Groceries", Decimal("10.00"))]

    result = CanonicalPurchaseReconciler().reconcile(purchase, items, splits)

    assert result.category_splits.status is ReconciliationStatus.PASS
    assert result.category_splits.expected == Decimal("10.00")
    assert result.category_splits.actual == Decimal("10.00")


def test_reconciliation_is_repeatable_for_same_canonical_purchase():
    purchase = CanonicalPurchaseAmounts(
        expense_pk=42,
        subtotal=Decimal("10.00"),
        total=Decimal("10.00"),
        receipt_equation_complete=True,
    )
    items = [CanonicalPurchaseItem(1, 42, Decimal("10.00"), "Groceries")]
    splits = [CategorySplit(42, "Groceries", Decimal("10.00"))]
    reconciler = CanonicalPurchaseReconciler()

    first = reconciler.reconcile(purchase, items, splits)
    second = reconciler.reconcile(purchase, items, splits)

    assert first == second


def test_rejects_data_from_another_canonical_expense():
    purchase = CanonicalPurchaseAmounts(
        expense_pk=42,
        subtotal=Decimal("10.00"),
        total=Decimal("10.00"),
    )
    items = [CanonicalPurchaseItem(1, 99, Decimal("10.00"), "Groceries")]

    with pytest.raises(ValueError, match="all items must belong"):
        CanonicalPurchaseReconciler().reconcile(purchase, items, [])


def test_rejects_negative_tolerance():
    with pytest.raises(ValueError, match="tolerance must be non-negative"):
        CanonicalPurchaseReconciler(tolerance=Decimal("-0.01"))
