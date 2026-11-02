"""Synthetic source-layout checks; no receipt-specific expected-value rules."""
import copy
import json
from types import SimpleNamespace

import pytest

from home_budget_pipeline.receipts import departments, ingest, parallel_ingest, ocr_results, evidence
from home_budget_pipeline.receipts.store_contact import extract_store_contact
from home_budget_pipeline.web.app import _post_ocr_text
from test_ocr_results_collector import result_receipt


def layout_line(text, x=140, height=32):
    return ingest.OCRLine(text, 95, x, 100, 400, height) if x is not None else ingest.OCRLine(text, 95)


def test_geometry_controls_membership_and_supporting_lines():
    lines = [layout_line('MEAT', 100), layout_line('Ground product 12.00', 112),
             layout_line('BAKERY', 105), layout_line('Bread 5.00', 145),
             layout_line('INSTANT SAVINGS 1.00', 145), layout_line('2 @ 2/5.00', 155),
             layout_line('Other 3.00', 40), layout_line('Unrelated 2.00', 145),
             layout_line('TOTAL 21.00', 200), layout_line('Footer 1.00', 145)]
    pages = parallel_ingest._cache_layout_pages([lines])
    rows = list(departments.walk(pages[0]['lines']))
    assert [r['department'] for r in rows] == [None, 'MEAT', None, 'BAKERY', 'BAKERY', 'BAKERY', None, None, None, None]
    items = ingest.extract_items('\n'.join(l.text for l in lines))
    amounts = [i.line_total for i in items]
    departments.assign_items(items, pages)
    assert [(i.item_name, i.department) for i in items] == [('Ground product', 'MEAT'), ('Bread', 'BAKERY'), ('Other', None), ('Unrelated', None)]
    assert [i.line_total for i in items] == amounts == [12, 4, 3, 2]


def test_missing_geometry_and_new_page_do_not_inherit_department():
    pages = parallel_ingest._cache_layout_pages([
        [layout_line('BAKERY', 100), layout_line('Bread 5.00', None)],
        [layout_line('Bagel 3.00', 145)]])
    assert all(l.get('department') is None for p in pages for l in departments.walk(p['lines']))


def test_legacy_cached_order_only_labels_are_rechecked_without_mutation():
    original = [{'page_number': 1, 'lines': [{'text':'BAKERY', 'children':[
        {'text':'Bread 5.00', 'department':'BAKERY'}]}]}]
    before = copy.deepcopy(original)
    result = departments.label_pages(original)
    assert list(departments.walk(result[0]['lines']))[1]['department'] is None
    assert original == before


def test_conflicting_department_readings_do_not_pick_first_or_guess_after_edit():
    pages = parallel_ingest._cache_layout_pages([[layout_line('MEAT', 100), layout_line('Product 5.00'),
        layout_line('BAKERY', 100), layout_line('Product 5.00')]])
    items = [ingest.ScannedItem('Product', 5)]
    departments.assign_items(items, pages)
    assert items[0].department is None
    source = [{'item_name':'Bread','line_total':5,'department':'BAKERY'}]
    assert departments.item_department({'item_name':'Bread','line_total':'5.00'}, source) == 'BAKERY'
    assert departments.item_department({'item_name':'Bread','line_total':6}, source) is None
    assert ingest.normalize_item_signs([ingest.ScannedItem('Bread',5,'BAKERY')], -5)[0].department == 'BAKERY'


@pytest.mark.parametrize('line', ['Phone: (204) 555-0123', 'TEL 204.555.0123', '+1 204-555-0123'])
def test_phone_and_tax_registration_are_optional_printed_fields(line):
    value = extract_store_contact(f'Example Shop\n{line}\nPRODUCT 5.00\nTOTAL 5.00\nGST # 123 456 789 RT 0002')
    assert value == {'store_phone':'204-555-0123', 'tax_registration_number':'123456789RT0002'}


@pytest.mark.parametrize('text', ['Example Shop\nFax: 204-555-0123',
    'Example Shop\nBill to:\nPhone: 204-555-0123',
    'Example Shop\nTOTAL 5.00\nCustomer phone: 204-555-0123',
    'Example Shop\nMember 204-555-0123\nGST 0.50'])
def test_contact_does_not_guess_from_customer_fax_loyalty_or_tax_amount(text):
    assert extract_store_contact(text) == {'store_phone':None,'tax_registration_number':None}


def test_contact_conflicts_and_failed_observations_stay_empty():
    observations = [{'page_number':1,'status':'success','text':'Example Shop\nPhone: 204-555-0123\n123456789RT0001'},
                    {'page_number':1,'status':'success','text':'Example Shop\nPhone: 204-555-0124\n123456789RT0002'}]
    assert extract_store_contact('Example Shop', observations=observations) == {'store_phone':None,'tax_registration_number':None}
    observations[1]['status'] = 'failed'
    assert extract_store_contact('Example Shop', observations=observations)['store_phone'] == '204-555-0123'


def test_fresh_and_cached_paths_use_same_geometry_and_contact(tmp_path, monkeypatch):
    source = tmp_path/'scan.png'
    source.write_bytes(b'synthetic')
    lines = [layout_line('Example Shop',100),layout_line('204-555-0123',100),
             layout_line('BAKERY',100),layout_line('Bread 5.00',140),layout_line('TOTAL 5.00',200),
             layout_line('123456789RT0001',100)]
    text = '\n'.join(l.text for l in lines)
    monkeypatch.setattr(ingest,'extract_page_text',lambda *a: [text])
    monkeypatch.setattr(ingest,'extract_page_layout',lambda *a: [lines])
    fresh = ingest.parse_scan(source,tmp_path)
    monkeypatch.setattr(parallel_ingest,'_read_or_create_pages',lambda *a,**kw: ([text],True))
    monkeypatch.setattr(parallel_ingest,'_ocr_run_metadata',lambda *a: {})
    monkeypatch.setattr(parallel_ingest,'_structured_layout',lambda *a: parallel_ingest._cache_layout_pages([lines]))
    cached, _ = parallel_ingest._parse_scan_cached(source,tmp_path,tmp_path/'cache',False)
    for value in (fresh,cached):
        assert value.store_phone == '204-555-0123'
        assert value.tax_registration_number == '123456789RT0001'
        assert value.items[0].department == 'BAKERY'


def test_collector_roundtrip_persistence_and_display(tmp_path, monkeypatch):
    source = tmp_path/'scan.png'
    source.write_bytes(b'synthetic')
    receipt = result_receipt(source)
    receipt.store_phone = '204-555-0123'
    receipt.tax_registration_number = '123456789RT0001'
    receipt.items[0].department = 'GROCERY'
    event = ocr_results.result_messages_for_receipt(receipt)[0]
    restored = ocr_results.receipt_from_message(event)
    saved = []
    class Cursor:
        def __enter__(self): return self
        def __exit__(self,*a): pass
        def execute(self,sql,params): saved.append(params)
        def fetchone(self): return (42,)
    for name in ('persist_ocr_lines','persist_ocr_run','persist_auto_annotations'):
        monkeypatch.setattr(evidence,name,lambda *a: None)
    evidence.upsert_evidence(SimpleNamespace(cursor=Cursor),restored)
    payload = json.loads(saved[0][-1])
    assert payload['items'][0]['department'] == 'GROCERY'
    assert payload['store_phone'] == receipt.store_phone
    assert payload['tax_registration_number'] == receipt.tax_registration_number
    summary = _post_ocr_text({'store_phone':restored.store_phone,'tax_registration_number':restored.tax_registration_number,'raw_payload':payload},None)
    assert 'Phone: 204-555-0123' in summary
    assert 'Tax registration (RT): 123456789RT0001' in summary
    assert '[GROCERY]' in summary
    for key in ('store_phone','tax_registration_number'):
        event.payload['receipt'].pop(key)
    for item in event.payload['receipt']['items']:
        item.pop('department')
    legacy = ocr_results.receipt_from_message(event)
    assert legacy.store_phone is None and legacy.items[0].department is None
