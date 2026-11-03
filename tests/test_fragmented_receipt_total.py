"""Broken monetary tokens need corroboration, not a plausible decimal suffix."""
import pytest
from home_budget_pipeline.receipts.ingest import extract_totals


def terminal(tender='246.80', purchase='246.80', extra=''):
    return f'Mastercard TENDER ${tender}\nCHANGE $0.00\n{extra}\nPurchase ${purchase}\nCARD MC\nAPPROVED'


def test_fragmented_total_uses_agreeing_tender_and_terminal_purchase():
    text = 'SUBTOTAL $240.00\nGST $6.80\nTOT AL p24 6.80.\n' + terminal()
    assert extract_totals(text) == (240.00,6.80,246.80)


@pytest.mark.parametrize('payments', [
    terminal(purchase='245.80'),
    'Mastercard TENDER $246.80',
    'Purchase $246.80\nCARD MC',
    terminal(extra='Visa TENDER $100.00'),
    terminal(extra='CHANGE $5.00'),
    terminal(extra='Cash back $20.00'),
    'Mastercard TENDER $246.80\nPurchase book $246.80',
])
def test_uncorroborated_or_split_payments_do_not_repair_fragmented_total(payments):
    assert extract_totals('TOTAL p24 6.80\n'+payments)[2] is None


def test_clear_total_is_not_overwritten_by_payment_amounts():
    assert extract_totals('TOTAL $250.00\n'+terminal())[2] == 250.00


def test_repeated_payment_rows_do_not_become_extra_payments():
    assert extract_totals('TOTAL p24 6.80\n'+terminal()+'\n'+terminal())[2] == 246.80


def test_unclear_other_totals_still_need_review():
    text='TOTAL p24 6.80\n'+terminal()+'\nPurchase $249.80\nCARD MC'
    assert extract_totals(text)[2] is None


def test_conflicting_complete_total_is_not_discarded_for_payment_corroboration():
    assert extract_totals('TOTAL $250.00\nTOTAL p24 6.80\n'+terminal())[2] is None
