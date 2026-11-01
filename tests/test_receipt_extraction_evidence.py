"""General extraction rules operate on observations, not corrected receipts."""
import pytest
from home_budget_pipeline.receipts import ingest as scan


def candidate(text, *, confidence=90, dpi=200, engine='tesseract'):
    lines = tuple(scan.OCRLine(line, confidence) for line in text.splitlines())
    return scan.OCRCandidate(text, lines, dpi, '6', engine)


@pytest.mark.parametrize('sku', ['123', '1234567', '123456789012'])
def test_split_product_and_amount_columns_preserve_duplicate_rows(sku):
    text = f'{sku} SAMPLE PRODUCT\n7.25 P\n{sku} SAMPLE PRODUCT\n7.25 P\nSUBTOTAL\n14.50\nTAX\n0.87\nTOTAL\n15.37'
    assert [(i.item_name, i.line_total) for i in scan.extract_items(text)] == [(f'{sku} SAMPLE PRODUCT',7.25)]*2
    assert scan.extract_totals(text) == (14.5,.87,15.37)


def test_split_named_tax_components_are_amounts_not_percentages():
    text = 'SUBTOTAL\n20.00\nG GST 5.00%\n1.00\nH PST 7%\n1.40\nTOTAL TAX\n2.40\nTOTAL\n22.40'
    assert scan.extract_totals(text, infer_tax=False) == (20.,2.4,22.4)
    assert scan.extract_tax_components(text) == {'gst':1.,'pst':1.4}
    assert scan.extract_tax_components('GST 5.00%\nRegistration 123456789RT') == {}
    assert scan.extract_totals('GST 5.00%', infer_tax=False)[1] is None


def test_observed_fields_outrank_noisy_text_and_inferred_balance():
    noisy = candidate('RANDOM TEXT '*100+'\n5/20/28 12:34\nSUBTOTAL 20.00\nTOTAL 22.00', confidence=99, dpi=450)
    clear = candidate('EXAMPLE SHOP\n5/20/28 12:34\nSUBTOTAL\n20.00\nTAX\n1.00\nTOTAL\n21.00')
    assert scan._ocr_candidate_score(noisy.text) > scan._ocr_candidate_score(clear.text)
    assert scan._select_ocr_candidate([noisy,clear]) is clear


def test_cross_pass_numeric_alignment_cannot_replace_coherent_base_header():
    clear = candidate('EXAMPLE SHOP\nSUBTOTAL\n20,00\nTAX\n1.00\nTOTAL\n21.00')
    bad = candidate('EXAMPLE SHOP\nSUBTOTAL\n10.00\nTAX\n1.00\nTOTAL\n21.00', confidence=99, engine='paddle')
    assert scan.extract_totals(scan._line_consensus_text([clear,bad]), infer_tax=False) == (20.,1.,21.)


@pytest.mark.parametrize('text', ['AMOUNT $21.00','AMOUNT: $21.00','PAYMENT $21.00','TENDERED $21.00'])
def test_tender_amount_is_never_a_product_without_a_subtotal_boundary(text):
    assert scan.extract_items(text) == []
    assert scan.extract_items('1234567 PRODUCT\n'+text) == []


def test_ambiguous_price_is_not_filled_from_the_other_item_or_subtotal():
    text = 'EXAMPLE SHOP\n5/20/28 12:34\n1234567 PRODUCT\n14.9\n1234567 PRODUCT\n14.99 P\nSUBTOTAL\n29.98\nTAX\n1.50\nTOTAL\n31.48'
    subtotal,tax,total=scan.extract_totals(text)
    items=scan.extract_items(text)
    assert [i.line_total for i in items] == [14.99]
    receipt=scan.ScannedReceipt(path='',source_reference='',source_sha256='a'*64,
        merchant=scan.extract_merchant(text),transaction_date=scan.extract_date(text),receipt_id='123456',
        subtotal=subtotal,tax=tax,total=total,items=items,text=text)
    receipt.extraction_confidence=scan.confidence_for(receipt)
    assert scan.status_for(receipt) == 'review'
    assert 'item_total_mismatch' in scan.review_reasons_for(receipt)


def test_generic_banner_does_not_name_an_unrelated_retailer():
    assert scan.extract_merchant('EXAMPLE SUPPLY\nWHOLESALE\n123 ANY STREET') == 'EXAMPLE SUPPLY'


def test_supplement_keeps_both_printed_tax_components_after_payment_is_known():
    primary='SUBTOTAL 20.00\nTOTAL 22.40\nVISA *1234 22.40'
    alternate='GST 1.00\nRST 1.40\nTOTAL 22.40'
    text=scan._supplement_missing_receipt_summary(primary,alternate)
    assert scan.extract_tax_components(text) == {'gst':1.,'pst':1.4}


def test_complete_observed_item_allocation_outranks_partial_high_confidence_pass():
    complete=candidate('EXAMPLE SHOP\n123 PRODUCT 12.00\n456 OTHER 8.00\nSUBTOTAL 20.00\nTAX 1.00\nTOTAL 21.00')
    partial=candidate('EXAMPLE SHOP\n123 PRODUCT 12.00\nSUBTOTAL 20.00\nTAX 1.00\nTOTAL 21.00',confidence=99,engine='paddle')
    assert scan._select_ocr_candidate([partial,complete]) is complete


def test_decimal_registration_identifier_does_not_become_tax():
    text='GST #123456.78 RT\nSUBTOTAL 10.00\nTOTAL 10.00'
    assert scan.extract_tax_components(text) == {}
    assert scan.extract_totals(text,infer_tax=False) == (10.,None,10.)


@pytest.mark.parametrize('merchant,expected', [('RANDOM HEADER','review'),('EXAMPLE SHOP','complete')])
def test_high_field_coverage_does_not_accept_uncorroborated_merchant(merchant,expected):
    text=merchant+'\n5/20/28 12:34\n123 PRODUCT 10.00\nSUBTOTAL 10.00\nTAX 0.50\nTOTAL 10.50'
    receipt=scan.ScannedReceipt(path='',source_reference='',source_sha256='a'*64,
        merchant=merchant,transaction_date='2028-05-20',receipt_id='123456',
        subtotal=10.,tax=.5,total=10.5,items=[scan.ScannedItem('PRODUCT',10.)],text=text)
    receipt.ocr_run={'ocr_passes':[{'page_number':1,'text':name+'\nTOTAL 10.50','status':'success'} for name in ['RANDOM HEADER','EXAMPLE SHOP','EXAMPLE SHOP']]}
    receipt.extraction_confidence=scan.confidence_for(receipt)
    assert receipt.extraction_confidence == 1.
    assert scan.status_for(receipt) == expected
    assert ('uncorroborated_merchant' in scan.review_reasons_for(receipt)) == (expected=='review')
