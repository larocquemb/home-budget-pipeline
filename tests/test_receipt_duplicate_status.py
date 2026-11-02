"""Duplicate identity is separate from processing and extraction review."""
from home_budget_pipeline.web import receipt_app
from home_budget_pipeline.web.receipt_fields import receipt_status


class Service:
    def __init__(self, duplicate=True):
        self.evidence = {'merchant':'Example Shop','extraction_status':'review','total':25,
            'raw_payload':{'duplicate_merge':{'canonical_expense_pk':22}} if duplicate else {}}
    def _fetch(self,*a):
        return ({'evidence_id':1,'expense_pk':22,'source_reference':'example.png',
                 'processing_status':'review_required'},)
    def receipt_evidence(self,*a): return self.evidence
    def expense_detail(self,*a): return {'expense_pk':22,'extraction_status':'review'}


def test_duplicate_receipt_shows_link_and_retains_raw_review_states():
    service = Service()
    data = receipt_app._receipt_detail(service,'a'*64)
    assert data['duplicate_of'] == 22 and data['receipt_status'] == 'duplicate'
    assert data['processing_status'] == 'review_required'
    assert data['extraction_status'] == 'review'
    html = receipt_app.receipt_page('a'*64,service=service,identity={'user':'test'})
    assert '<strong>Status</strong><br><a href="/ledger/expenses/22">Duplicate of 22</a>' in html
    assert '<strong>Extraction</strong>' not in html
    assert 'Historical extraction' in html
    assert 'This duplicate copy requires no review.' in html


def test_canonical_receipt_keeps_review_status_instead_of_becoming_duplicate():
    data = receipt_app._receipt_detail(Service(False),'a'*64)
    assert data['duplicate_of'] is None and receipt_status(data) == 'review_required'
    assert 'Duplicate of' not in receipt_app._receipt_status_html(data)


def test_retained_list_copy_and_canonical_link_use_correct_source():
    result = receipt_app._receipt_status_html({'duplicate_of':22,'processing_status':'review_required'})
    assert result == '<a href="/ledger/expenses/22">Duplicate of 22</a>'
    calls = []
    class Sources:
        def _fetch(self,sql,params):
            calls.append(sql)
            return ({'source_sha256':'b'*64},)
    assert receipt_app._receipt_for_expense(Sources(),22) == 'b'*64
    assert "? 'duplicate_merge'" in calls[0]


def test_duplicate_copy_has_no_active_item_review_actions(monkeypatch):
    data = {'source_reference':'duplicate.png','evidence_id':1,'expense_pk':22,
        'processing_status':'review_required','extraction_status':'review','duplicate_of':22,
        'evidence':{'possible_duplicate_expense_pks':[44],'raw_payload':{'duplicate_merge':{'canonical_expense_pk':22}}},
        'expense':{'items':[{'item_name':'Canonical product','line_total':25}]}}
    monkeypatch.setattr(receipt_app,'_receipt_detail',lambda *a: data)
    html = receipt_app.receipt_page('a'*64,service=object(),identity={'user':'test'})
    assert 'Duplicate of 22' in html
    assert 'Possible duplicate purchase' not in html
    assert 'id="refresh-items"' not in html
    assert 'id="allocation-correction"' not in html
    data['duplicate_of'] = None
    data['evidence']['raw_payload'] = {}
    canonical_html = receipt_app.receipt_page('a'*64,service=object(),identity={'user':'test'})
    assert 'id="refresh-items"' in canonical_html
    assert '<strong>Extraction</strong><br>review' in canonical_html
