from datetime import date
from decimal import Decimal

from home_budget_pipeline.transactions import TransactionCsvNormalizer, transaction_fingerprint


FIELD_MAP = {
    "transaction_date": "Transaction Date",
    "posting_date": "Posting Date",
    "amount": "Amount",
    "description": "Description",
    "merchant_text": "Merchant",
    "source_transaction_id": "Transaction ID",
    "card_last4": "Card",
}


def test_normalizes_institution_csv_without_source_editing():
    csv_text = """Transaction Date,Posting Date,Amount,Description,Merchant,Transaction ID,Card\n2026-08-18,2026-08-19,42.15,COSTCO WHOLESALE,COSTCO,abc123,1234\n"""
    normalizer = TransactionCsvNormalizer(
        institution="CIBC",
        source_account_reference="Aventura 1234",
        field_map=FIELD_MAP,
        amount_sign=Decimal("-1"),
    )
    [tx] = normalizer.normalize_text(csv_text)
    assert tx.transaction_date == date(2026, 8, 18)
    assert tx.posting_date == date(2026, 8, 19)
    assert tx.amount == Decimal("-42.15")
    assert tx.description == "COSTCO WHOLESALE"
    assert tx.merchant_text == "COSTCO"
    assert tx.source_transaction_id == "abc123"
    assert tx.card_last4 == "1234"
    assert tx.raw_source["Amount"] == "42.15"


def test_same_source_transaction_id_dedupes_across_overlapping_files():
    kwargs = dict(
        institution="CIBC",
        source_account_reference="Aventura 1234",
        transaction_date=date(2026, 8, 18),
        posting_date=date(2026, 8, 19),
        amount=Decimal("-42.15"),
        description="COSTCO WHOLESALE",
        source_transaction_id="abc123",
    )
    assert transaction_fingerprint(**kwargs) == transaction_fingerprint(**kwargs)


def test_fallback_fingerprint_is_stable_without_institution_transaction_id():
    first = transaction_fingerprint(
        institution="CIBC",
        source_account_reference="Chequing",
        transaction_date=date(2026, 8, 18),
        posting_date=date(2026, 8, 19),
        amount=Decimal("10.0"),
        description="  Sobeys   IDC ",
    )
    second = transaction_fingerprint(
        institution="cibc",
        source_account_reference="chequing",
        transaction_date=date(2026, 8, 18),
        posting_date=date(2026, 8, 19),
        amount=Decimal("10.00"),
        description="sobeys idc",
    )
    assert first == second


def test_different_same_day_transactions_remain_distinct_by_amount():
    base = dict(
        institution="CIBC",
        source_account_reference="Aventura 1234",
        transaction_date=date(2026, 8, 18),
        posting_date=date(2026, 8, 19),
        description="TIM HORTONS",
    )
    assert transaction_fingerprint(amount=Decimal("5.25"), **base) != transaction_fingerprint(
        amount=Decimal("8.75"), **base
    )


def test_preserves_transaction_and_posting_dates_separately():
    csv_text = """Transaction Date,Posting Date,Amount,Description,Merchant,Transaction ID,Card\n08/18/2026,08/20/2026,7.00,COFFEE SHOP,,,,\n"""
    [tx] = TransactionCsvNormalizer(
        institution="CIBC",
        source_account_reference="Chequing",
        field_map=FIELD_MAP,
    ).normalize_text(csv_text)
    assert tx.transaction_date == date(2026, 8, 18)
    assert tx.posting_date == date(2026, 8, 20)
