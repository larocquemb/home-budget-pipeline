from datetime import date
from decimal import Decimal

import pytest

from home_budget_pipeline.data_quality.required_fields import (
    CanonicalExpense,
    CanonicalExpenseItem,
    DataQualityStatus,
    RequiredFieldValidator,
)


def expense(**overrides):
    values = dict(
        expense_pk=1,
        source="scanned",
        order_id="receipt-1",
        order_date=date(2026, 8, 20),
        store_name="Sobeys",
        expense_total=Decimal("12.34"),
    )
    values.update(overrides)
    return CanonicalExpense(**values)


def item(**overrides):
    values = dict(id=10, expense_pk=1, item_name="Milk", line_total=Decimal("12.34"))
    values.update(overrides)
    return CanonicalExpenseItem(**values)


def test_valid_canonical_expense_passes():
    result = RequiredFieldValidator().validate(expense(), [item()])
    assert result.status is DataQualityStatus.PASS
    assert result.violations == ()
    assert result.requires_review is False


def test_missing_purchase_fields_are_reported_individually():
    result = RequiredFieldValidator().validate(
        expense(order_id=" ", order_date=None, store_name=None, expense_total=None),
        [item()],
    )
    assert result.status is DataQualityStatus.FAIL
    assert result.requires_review is True
    assert {v.rule for v in result.violations} == {
        "missing_order_id",
        "missing_transaction_date",
        "missing_store_name",
        "missing_expense_total",
    }


def test_transaction_datetime_can_satisfy_date_requirement():
    from datetime import datetime

    result = RequiredFieldValidator().validate(
        expense(order_date=None, transaction_datetime=datetime(2026, 8, 20, 12, 30)),
        [item()],
    )
    assert result.status is DataQualityStatus.PASS


def test_missing_items_fails():
    result = RequiredFieldValidator().validate(expense(), [])
    assert [v.rule for v in result.violations] == ["missing_items"]


def test_missing_item_fields_are_reported_with_item_id():
    result = RequiredFieldValidator().validate(
        expense(), [item(item_name="", line_total=None)]
    )
    assert [(v.rule, v.item_id) for v in result.violations] == [
        ("item_missing_name", 10),
        ("item_missing_line_total", 10),
    ]


def test_source_specific_rule_can_be_not_applicable():
    validator = RequiredFieldValidator()
    assert validator.source_rule_status("scanned", "instacart") is DataQualityStatus.NOT_APPLICABLE
    assert validator.source_rule_status("instacart", "instacart") is DataQualityStatus.PASS


def test_repeatability():
    validator = RequiredFieldValidator()
    first = validator.validate(expense(), [item()])
    second = validator.validate(expense(), [item()])
    assert first == second


def test_items_from_other_canonical_expense_are_rejected():
    with pytest.raises(ValueError, match="canonical expense"):
        RequiredFieldValidator().validate(expense(), [item(expense_pk=2)])
