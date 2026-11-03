from decimal import Decimal
import pytest

from home_budget_pipeline.transactions.cibc import parse_cibc
from home_budget_pipeline.transactions.workflows import normalize_allocations


def csv_row(day='2026-03-12', description='Market, Main', debit='12.34', credit='', card='1234********5678'):
    return f'{day},"{description}",{debit},{credit},{card}\r\n'.encode()


def test_cibc_headerless_first_row_bom_quotes_and_signed_credits():
    rows = parse_cibc(b'\xef\xbb\xbf' + csv_row() + csv_row(debit='', credit='8.12', description='REFUND'), 'Card account')
    assert len(rows) == 2
    assert rows[0].description == 'Market, Main'
    assert rows[0].amount == Decimal('12.34')
    assert rows[1].amount == Decimal('-8.12')
    assert rows[1].kind == 'credit'
    assert rows[0].raw[4] == '1234********5678'


def test_overlap_fingerprints_preserve_identical_occurrences_and_card_identity():
    a = parse_cibc(csv_row()*2 + csv_row(card='1234********9012'), 'Card account')
    b = parse_cibc(csv_row(card='1234********9012') + csv_row()*2, 'Card account')
    assert len({r.fingerprint for r in a}) == 3
    assert {r.fingerprint for r in a} == {r.fingerprint for r in b}
    assert parse_cibc(csv_row(), 'different account')[0].fingerprint != a[0].fingerprint


@pytest.mark.parametrize('data', [b'', b'Date,Description,Debit,Credit,Card\n', csv_row(day='2026-02-30'),
    csv_row(debit='NaN'), csv_row(debit='-1.00'), csv_row(debit='1.001'),
    csv_row(debit='0.00'), csv_row(debit='1.00',credit='1.00'),
    csv_row(debit='',credit=''), csv_row(card='1234567890123456'), csv_row(description='')])
def test_cibc_rejects_invalid_rows_without_partial_import(data):
    with pytest.raises(ValueError):
        parse_cibc(data, 'Card account')


def test_payment_is_separate_from_refund():
    row = parse_cibc(csv_row(description='PAYMENT THANK YOU', debit='',credit='75.00'), 'Card account')[0]
    assert row.kind == 'payment'


def test_full_account_number_is_not_an_allowed_account_reference():
    with pytest.raises(ValueError,match='full account number'):
        parse_cibc(csv_row(),'1234567890123456')


def test_accounting_connection_never_falls_back_to_receipt_database(monkeypatch):
    from home_budget_pipeline.transactions.database import connect
    monkeypatch.delenv('ACCOUNTING_DATABASE_URL',raising=False)
    with pytest.raises(ValueError,match='required'):
        connect()
    monkeypatch.setenv('DATABASE_URL','postgresql://user:password@example.invalid/receipts')
    monkeypatch.setenv('ACCOUNTING_DATABASE_URL','postgresql://user:password@example.invalid/receipts')
    with pytest.raises(ValueError,match='separate databases'):
        connect()


def test_balanced_split_and_refund_use_decimal_cents():
    rows = normalize_allocations([{'amount':'0.10','category':'Food'}, {'amount':'0.20','category':'Home','expense_pk':5}], Decimal('.30'))
    assert sum(r['amount'] for r in rows) == Decimal('.30')
    assert normalize_allocations([{'amount':'-5.25','category':'Refund'}], Decimal('-5.25'))[0]['amount'] == Decimal('-5.25')


@pytest.mark.parametrize('rows', [[], [{'amount':'1.00','category':'Food'}],
    [{'amount':'-2.00','category':'Food'}], [{'amount':'2.001','category':'Food'}],
    [{'amount':'NaN','category':'Food'}], [{'amount':'2.00','category':''}],
    [{'amount':'2.00','category':'Food','expense_pk':True}],
    [{'amount':'2.00','category':'Food','extra':'ignored?'}]])
def test_split_rejects_unbalanced_or_invalid_values(rows):
    with pytest.raises(ValueError):
        normalize_allocations(rows, Decimal('2.00'))
