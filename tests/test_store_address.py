"""Printed store labels and addresses stay separate from brands and customers."""
from home_budget_pipeline.receipts import ingest, parallel_ingest, ocr_results
from home_budget_pipeline.web.app import _post_ocr_text
from test_ocr_results_collector import result_receipt


def test_full_branch_header_is_retained_after_separate_brand_logo():
    text = 'ExampleMart\nExampleMart Riverside\n120 Riverbank Boulevard\nServed by: Operator 2'
    assert ingest.extract_merchant_store(text) == ('ExampleMart', 'ExampleMart Riverside')
    assert ingest.extract_store_address(text) == '120 Riverbank Boulevard'


def test_header_location_does_not_rely_on_one_retailer_alias():
    for brand in ('Example Foods', 'Sample Hardware', 'Other Retailer'):
        text = f'{brand}\n{brand} West End\n75 West Road'
        assert ingest.extract_merchant_store(text) == (brand, brand + ' West End')
        assert ingest.extract_store_address(text) == '75 West Road'


def test_store_address_never_comes_from_customer_or_delivery_section():
    for boundary in ('Bill to:', 'Ship to:', 'Sold to:', 'Customer address:'):
        text = f'ExampleMart\n{boundary}\n120 Riverbank Boulevard'
        assert ingest.extract_store_address(text) is None
        assert ingest.extract_merchant_store(text)[1] is None


def test_address_is_not_a_merchant_and_header_does_not_take_footer_addresses():
    text = 'ExampleMart\n120 Riverbank Boulevard\nTOTAL 24.60\n999 Other Road'
    assert ingest.extract_merchant_store(text) == ('ExampleMart', None)
    assert ingest.extract_store_address(text) == '120 Riverbank Boulevard'


def test_retained_header_readings_can_supply_missing_store_and_address():
    text = 'ExampleMart\nTOTAL 24.60'
    observations = [{'page_number': 1, 'status': 'success', 'provenance': {'lines': [
        {'text': row} for row in 'ExampleMart\nExampleMart Riverside\n120 Riverbank Boulevard'.splitlines()]}}]
    assert ingest.extract_merchant_store(text, observations=observations) == ('ExampleMart', 'ExampleMart Riverside')
    assert ingest.extract_store_address(text, observations=observations) == '120 Riverbank Boulevard'
    observations[0]['status'] = 'failed'
    assert ingest.extract_store_address(text, observations=observations) is None


def test_conflicting_alternative_headers_do_not_choose_first():
    observations = [{'page_number': 1, 'status': 'success', 'text': f'ExampleMart\nExampleMart {branch}\n{address}'}
        for branch, address in [('Riverside', '120 Riverbank Boulevard'), ('West End', '75 West Road')]]
    assert ingest.extract_store_address('ExampleMart', observations=observations) is None
    assert ingest.extract_merchant_store('ExampleMart', observations=observations)[1] is None


def test_store_address_survives_result_collector_and_summary_rendering(tmp_path):
    source = tmp_path / 'receipt.pdf'
    source.write_bytes(b'synthetic')
    receipt = result_receipt(source)
    receipt.store = 'ExampleMart Riverside'
    receipt.store_address = '120 Riverbank Boulevard'
    event = ocr_results.result_messages_for_receipt(receipt)[0]
    restored = ocr_results.receipt_from_message(event)
    assert restored.store_address == receipt.store_address
    text = _post_ocr_text({'store': restored.store, 'store_address': restored.store_address}, None)
    assert 'Store: ExampleMart Riverside' in text
    assert 'Address: 120 Riverbank Boulevard' in text
    event.payload['receipt'].pop('store_address')
    assert ocr_results.receipt_from_message(event).store_address is None


def test_fresh_and_cached_parsers_extract_same_location(tmp_path, monkeypatch):
    source = tmp_path / 'receipt.png'
    source.write_bytes(b'unchanged')
    text = 'ExampleMart\nExampleMart Riverside\n120 Riverbank Boulevard\nPRODUCT 24.60\nTOTAL 24.60'
    monkeypatch.setattr(ingest, 'extract_page_text', lambda *a, **kw: [text])
    direct = ingest.parse_scan(source, tmp_path)
    monkeypatch.setattr(parallel_ingest, '_read_or_create_pages', lambda *a, **kw: ([text], True))
    monkeypatch.setattr(parallel_ingest, '_ocr_run_metadata', lambda *a: {})
    monkeypatch.setattr(parallel_ingest, '_structured_layout', lambda *a: [])
    cached, _ = parallel_ingest._parse_scan_cached(source, tmp_path, tmp_path / 'cache', False)
    assert direct.store == cached.store == 'ExampleMart Riverside'
    assert direct.store_address == cached.store_address == '120 Riverbank Boulevard'
