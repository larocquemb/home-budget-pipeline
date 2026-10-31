"""Synthetic receipts exercise allocation rules without private source data."""
from dataclasses import asdict

import pytest

from home_budget_pipeline.receipts import ingest as scan
from home_budget_pipeline.receipts.allocation import assess, money
from home_budget_pipeline.web.allocation_corrections import normalize


def test_balanced_header_does_not_prove_item_allocations():
    check = assess([{'line_total': '7.10'}], '7.00', '.35', '7.35')
    assert not check['reconciled']
    assert check['reasons'] == ['item_total_mismatch']
    assert check['header_difference'] == '0.00'


def test_included_fuel_tax_is_not_added_and_pump_details_are_not_items():
    text = '1 PREMIUM GASOLINE $ 2.123 $ 42.46\nPump: 2\nLitres: 20.000\nPrice / Litre: $ 2.123\nSubtotal $42.46\nGST [Inc Pumps] $2.02\nTotal $42.46'
    subtotal, tax, total = scan.extract_totals(text)
    assert (subtotal, tax, total) == (42.46, 0, 42.46)
    items = scan.extract_items(text)
    assert [(i.item_name, i.line_total) for i in items] == [('PREMIUM GASOLINE', 42.46)]
    assert assess([asdict(i) for i in items], subtotal, tax, total)['reconciled']


def test_split_fuel_amount_and_additional_shop_tax():
    text = '1 REGULAR GASOLINE\n1 COOKIE $2.500 $2.50\nPump:\n$1.500 30.00\nLitres: 20.000\nSubtotal $32.50\nGST $0.13\nGST [Incl Pumps] $1.43\nTotal $32.63'
    assert sorted(i.line_total for i in scan.extract_items(text)) == [2.5, 30]
    assert scan.extract_totals(text) == (32.5, .13, 32.63)


def test_loyalty_allocations_and_tender_are_not_additional_purchases():
    text = 'CASUAL SHIRT\n12345678 1 ea 20.00 12.00\n$5 Off Loyalty Appeasement -2.00\nFiscal 2027 Loyalty Certs $5 -2.00\nSubtotal 12.00\nTax 1.44\nTotal 13.44\nxx Purchase 13.44'
    assert [(i.item_name, i.line_total) for i in scan.extract_items(text)] == [('CASUAL SHIRT', 12)]


def test_discount_and_fee_apply_once_but_savings_summary_does_not():
    text = 'JUICE 4.00\nINSTANT SAVINGS -0.75\n+EHC 0.15\nYOU SAVED 2.00\nSubtotal 3.40\nTotal 3.40'
    assert [(i.item_name, i.line_total) for i in scan.extract_items(text)] == [('JUICE', 3.4)]


def test_repeated_tax_summary_is_not_a_second_tax():
    text = 'Subtotal 10.00\nGST 0.50\nPST 0.70\nTotal 11.20\nCOPY\nSubtotal 10.00\nGST 0.50\nPST 0.70\nTotal 11.20'
    assert scan.extract_totals(text) == (10, 1.2, 11.2)


def test_fuzzy_page_overlap_keeps_genuine_repeated_products():
    pages = ['SHOP\nMEAT\nCHICKEN 8.00\nPORK 6.00\nYOU SAVED 1.00',
             'MEAI\nCHICKEN 8.00\nPORK 6.00\nYOU SAVED 1.00\nDELI\nCHEESE 3.00\nCHEESE 3.00\nSubtotal 20.00']
    items = scan.extract_items(scan.merge_page_text(pages))
    assert [i.item_name for i in items] == ['CHICKEN', 'PORK', 'CHEESE', 'CHEESE']


@pytest.mark.parametrize('amount', ['NaN', 'Infinity', '.001', None])
def test_invalid_or_missing_amounts_cannot_reconcile(amount):
    assert not assess([{'line_total': amount}], 1, 0, 1)['reconciled']


def test_correction_rejects_attempt_to_change_receipt_total():
    with pytest.raises(ValueError, match='Unsupported'):
        normalize({'expense_total': '1.00'})
