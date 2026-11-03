"""Printed deductions apply to their product; savings summaries are informational."""
import pytest
from home_budget_pipeline.receipts.ingest import extract_items


def values(text):
    return [(i.item_name,i.line_total) for i in extract_items(text)]


@pytest.mark.parametrize('label',['INSTANT SAVINGS','TNSTANT SAVINGS','1NSTANT SAVINGS','lNSTANT SAVINGS'])
@pytest.mark.parametrize('discount',[' $1.25','\n-$1.25'])
def test_inline_and_wrapped_discount_with_ocr_initial_glyph(label,discount):
    assert values('Tomato Sauce $6.20\n'+label+discount+'\nYOU SAVED $2.00\nSubtotal 4.95') == [('Tomato Sauce',4.95)]


def test_discount_does_not_cross_an_unpriced_product():
    assert values('Earlier Product $6.20\nMissing Product\nINSTANT SAVINGS $1.25') == [('Earlier Product',6.20)]


def test_named_product_wrapped_amount_receives_its_own_discount():
    assert values('Earlier Product $6.20\nCream Cheese\n$4.80 C\nINSTANT SAVINGS\n-$0.60\nYOU SAVED $1.20') == [('Earlier Product',6.20),('Cream Cheese',4.20)]


def test_missing_discount_amount_is_not_borrowed_from_next_product():
    assert values('First Sauce $6.20\nINSTANT SAVINGS\nSecond Sauce $5.60\nINSTANT SAVINGS $1.25') == [('First Sauce',6.20),('Second Sauce',4.35)]


def test_two_identical_purchases_keep_their_own_discount():
    assert values(('Tomato Sauce $6.20\nINSTANT SAVINGS $1.25\nYOU SAVED $2.00\n')*2) == [('Tomato Sauce',4.95),('Tomato Sauce',4.95)]


def test_unit_price_and_savings_summary_are_not_purchases_or_extra_deductions():
    assert values('Fresh Apples $6.20\n1.250 kg 0 $4.96 / kg\nYOU SAVED $2.00\nINSTANT SAVINGS $1.25') == [('Fresh Apples',4.95)]


def test_section_heading_does_not_inherit_previous_adjustment_target():
    assert values('Earlier Product $6.20\nBAKERY\nINSTANT SAVINGS $1.25') == [('Earlier Product',6.20)]


def test_refund_discount_preserves_direction():
    assert values('Returned Product $6.20-\nINSTANT SAVINGS $1.25') == [('Returned Product',-4.95)]


@pytest.mark.parametrize('text',[
    'Earlier Product $6.20\nMissing Product\nINSTANT SAVINGS $1.25',
    'Earlier Product $6.20\nINSTANT SAVINGS',
    'Earlier Product $6.20\nINSTANT SAVINGS $9.00',
])
def test_unresolved_or_excessive_discount_is_explicit_and_does_not_change_previous_amount(text):
    issues=[]
    assert [(i.item_name,i.line_total) for i in extract_items(text,issues=issues)] == [('Earlier Product',6.20)]
    assert issues == ['unresolved_item_adjustment']


def test_unresolved_discount_requires_review_even_when_header_and_items_balance():
    from home_budget_pipeline.receipts.ingest import ScannedReceipt,review_reasons_for
    text='Earlier Product $6.20\nINSTANT SAVINGS\nSubtotal $6.20\nGST $0.31\nTotal $6.51'
    receipt=ScannedReceipt(path='',source_reference='example.png',source_sha256='a'*64,
        merchant='Example Shop',transaction_date='2031-07-12',receipt_id=None,
        subtotal=6.20,tax=.31,total=6.51,items=extract_items(text),text=text,extraction_confidence=.9)
    assert 'unresolved_item_adjustment' in review_reasons_for(receipt)
