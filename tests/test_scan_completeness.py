"""Bad scan joins are warnings, not permission to fabricate omitted purchases."""
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace

from home_budget_pipeline.receipts import ingest
from home_budget_pipeline.receipts.scan_completeness import apply,compare
from home_budget_pipeline.web.scan_completeness_html import render
from home_budget_pipeline.web.queries import LedgerQueryService


def receipt(pages,subtotal=20,total=21):
    return ingest.ScannedReceipt(path='',source_reference='example.pdf',source_sha256='a'*64,
        merchant='Example Shop',transaction_date='2031-07-12',receipt_id=None,subtotal=subtotal,
        tax=1,total=total,text='\n'.join(pages),items=ingest.extract_items(ingest.merge_page_text(pages)))


def test_missing_overlap_and_unreconciled_scan_is_explicit():
    pages=['First Sauce $4.20\nGreek Yogurt $8.40','Bread Rolls $3.20\nSUBTOTAL $20.00\nTAX $1.00\nTOTAL $21.00']
    r=receipt(pages);before=deepcopy(r.items);apply(r,pages,[])
    d=r.field_decisions['scan_completeness']
    assert d['status']=='conflict' and d['value']=='possible_page_gap'
    assert d['joins'][0]['overlap_detected'] is False
    assert r.items==before
    r.extraction_confidence=.9
    assert 'possible_missing_page_content' in ingest.review_reasons_for(r)


def test_observed_overlap_is_not_a_missing_source_gap_even_when_ocr_prices_disagree():
    pages=['Roasted Ham $6.20\nCheddar Dip $4.80','Roasted Ham $6.70\nCheddar Dip $4.80\nBread Rolls $3.20\nSUBTOTAL $20.00']
    r=receipt(pages);apply(r,pages,[])
    assert r.field_decisions['scan_completeness']['value']=='overlap_observed'


def test_reconciled_pages_without_overlap_are_not_claimed_incomplete():
    pages=['First Sauce $4.20','Bread Rolls $3.20\nSUBTOTAL $7.40\nTAX $1.00\nTOTAL $8.40']
    r=receipt(pages,7.40,8.40);apply(r,pages,[])
    assert r.field_decisions['scan_completeness']['value']=='financially_reconciled'


def test_electronic_documents_are_not_assumed_to_need_scanner_overlap():
    pages=['First Sauce $4.20','Bread Rolls $3.20\nSUBTOTAL $20.00']
    r=receipt(pages);r.source_reference='electronic/example.pdf';apply(r,pages,[])
    assert 'scan_completeness' not in r.field_decisions


def test_reference_identifies_contiguous_gap_between_page_anchors_without_inserting_rows():
    reference=[{'item_name':n,'line_total':v} for n,v in [('First Sauce',4.20),('Greek Yogurt',8.40),
        ('Roasted Ham',6.20),('Cheddar Dip',4.20),('Bread Rolls',3.20)]]
    pages=['First Sauce $4.20\nGreek Yogurt $8.40','Bread Rolls $3.20\nSUBTOTAL $26.20']
    original=deepcopy(reference);result=compare(reference,pages)
    assert [r['item_name'] for r in result['page_gaps'][0]['missing_reference_items']]==['Roasted Ham','Cheddar Dip']
    assert result['page_gaps'][0]['left_anchor']=='Greek Yogurt'
    assert result['page_gaps'][0]['right_anchor']=='Bread Rolls'
    assert reference==original


def test_price_disagreement_is_reported_without_changing_either_copy():
    reference=[{'item_name':'Greek Yogurt','line_total':8.40}]
    current=[{'item_name':'Greek Yogurt','line_total':68.40}]
    report=compare(reference,['Greek Yogurt 68.40'],current)
    assert report['price_differences'][0]['current_value']=='68.4'
    assert current[0]['line_total']==68.40


def test_detail_warning_and_reference_link_escape_ocr_text():
    data={'evidence':{},'source_comparison':{'reference_sha256':'b'*64,
        'reference_expense_pk':12,'matched_rows':3,'reference_rows':5,
        'page_gaps':[{'left_page':1,'right_page':2,
                      'missing_reference_items':[{'item_name':'<script>unsafe</script>'}]}]}}
    html=render(data,'/ledger')
    assert 'Possible missing scan content' in html and 'Expense 12' in html
    assert '<script>' not in html and '&lt;script&gt;' in html
    assert 'not added to this receipt' in html


def test_reference_query_only_uses_reconciled_retained_sources_and_safe_parameters():
    service=LedgerQueryService(lambda:None)
    calls=[]
    def fetch(sql,params):calls.append((sql,params));return []
    service._fetch=fetch
    assert service.receipt_source_comparison({'source_sha256':'a'*64,'merchant':'Example Shop',
        'transaction_datetime':datetime(2031,7,12,9,42,13),'card_last4':None}) is None
    sql,params=calls[0]
    assert "extraction_status='complete'" in sql and "? 'receipt_deleted'" in sql
    assert params[0]=='a'*64 and 'Example Shop' not in sql


def test_legacy_flattened_text_can_show_a_gap_without_fabricating_page_numbers():
    reference=[{'item_name':n,'line_total':v} for n,v in [('First Sauce',4.20),('Greek Yogurt',8.40),
        ('Roasted Ham',6.20),('Cheddar Dip',4.20),('Bread Rolls',3.20)]]
    report=compare(reference,['First Sauce $4.20\nGreek Yogurt $8.40\nBread Rolls $3.20\nSUBTOTAL 26.20'])
    assert report['page_boundaries_available'] is False
    assert report['page_gaps'][0]['left_page'] is None
    assert [r['item_name'] for r in report['page_gaps'][0]['missing_reference_items']]==['Roasted Ham','Cheddar Dip']


def test_unmatched_ocr_names_are_not_counted_as_missing_source_rows():
    reference=[{'item_name':n,'line_total':1} for n in
               ['First Sauce','Greek Yogurt','Roasted Ham','Cheddar Dip','Bread Rolls']]
    report=compare(reference,['First Sauce $1.00\nGreek Yogurt $1.00\nUnreadable Alpha $1.00\nUnreadable Beta $1.00\nBread Rolls $1.00'])
    assert report['page_gaps']==[]
