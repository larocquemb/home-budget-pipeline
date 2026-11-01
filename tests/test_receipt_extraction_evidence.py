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


def test_slash_tax_names_are_aliases_not_duplicate_or_missing_components():
    text='SUBTOTAL 20.00\nGST/HST 1.00\nPST/QST 1.40\nTOTAL 22.40'
    assert scan.extract_totals(text,infer_tax=False) == (20.,2.4,22.4)
    assert scan.extract_tax_components(text) == {'gst':1.,'pst':1.4}


def test_refund_tax_label_with_registration_and_split_value_keeps_sign():
    text='RETURNS\nSUBTOTAL\n20.00-\nGST R123456789 5%\n1.00-\nRST 7%\n1.40-\nTOTAL\n22.40-'
    assert scan.extract_totals(text,infer_tax=False) == (-20.,-2.4,-22.4)
    assert scan.extract_tax_components(text) == {'gst':-1.,'pst':-1.4}


def test_supplement_does_not_add_tax_again_to_coherent_included_tax_header():
    primary='SUBTOTAL 20.00\nGST (Incl) 1.00\nTOTAL 20.00'
    alternative='GST (Ine) 1.00\nTOTAL 20.00'
    result=scan._supplement_missing_receipt_summary(primary,alternative)
    assert scan.extract_totals(result,infer_tax=False) == (20.,0.,20.)


def test_tsv_engine_metadata_cannot_supply_prices_or_tax():
    tsv='level page_num block_num par_num line_num word_num left top width height conf text\n5 1 1 1 1 1 10 20 30 40 99.123456 GST'
    primary='EXAMPLE SHOP\nSUBTOTAL 20.00\nTAX 1.00\nTOTAL 21.00'
    assert scan._looks_like_tesseract_tsv(tsv)
    assert scan._supplement_missing_receipt_summary(primary,tsv) == primary
    assert scan._select_ocr_candidate([candidate(tsv,confidence=100),candidate(primary)]).text == primary


def test_refund_total_support_and_signed_components_survive_alternatives():
    positive=candidate('EXAMPLE SHOP\n123 PRODUCT 20.00-\nSUBTOTAL 20.00-\nGST 1.00\nPST 1.40\nTOTAL 22.40',confidence=99,dpi=450)
    negative=candidate('EXAMPLE SHOP\n123 PRODUCT 20.00-\nSUBTOTAL 20.00-\nTOTAL 22.40-')
    partial=candidate('EXAMPLE SHOP\nGST 1.00-\nPST 1.40-\nTOTAL 22.40-',confidence=95)
    text=scan.select_ocr_page([positive,negative,partial])
    assert scan.extract_totals(text,infer_tax=False) == (-20.,-2.4,-22.4)
    assert scan.extract_tax_components(text) == {'gst':-1.,'pst':-1.4}


def test_consistent_refund_direction_outranks_balanced_items_with_positive_total():
    wrong_direction=candidate('EXAMPLE SHOP\n123 PRODUCT 20.00-\nSUBTOTAL 20.00-\nGST 1.00\nTOTAL 22.40',confidence=99,dpi=450)
    negative=candidate('EXAMPLE SHOP\n123 PRODUCT 19.98-\nSUBTOTAL 20.00-\nTOTAL 22.40-')
    support=candidate('EXAMPLE SHOP\nGST 1.00-\nPST 1.40-\nTOTAL 22.40-')
    selected=scan._select_ocr_candidate([wrong_direction,negative,support])
    assert selected is not wrong_direction
    assert scan.extract_totals(selected.text,infer_tax=False)[2] == -22.4
