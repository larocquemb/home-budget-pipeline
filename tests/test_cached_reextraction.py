"""Cached extraction must preserve evidence and never fall back to OCR."""
import json
from types import SimpleNamespace

import pytest

from home_budget_pipeline.receipts import ingest, parallel_ingest, queue_ingest, ocr_collector
from home_budget_pipeline.receipts.message import ReceiptMessage


@pytest.mark.parametrize('cache_state', ['missing', 'invalid', 'wrong_source', 'missing_run', 'valid'])
def test_cached_reextraction_requires_matching_cache_and_preserves_ocr(tmp_path, monkeypatch, cache_state):
    source = tmp_path / 'receipt.pdf'
    source.write_bytes(b'original evidence')
    cache = tmp_path / 'cache'
    cache.mkdir()
    digest = ingest.sha256_file(source)
    cache_path = cache / 'receipt.pdf.json'
    payload = {'metadata': {'source_sha256': digest, 'cache_version': 1,
                           'run_uuid': '11111111-1111-4111-8111-111111111111',
                           'ocr_passes': []},
               'page_text': ['SYNTHETIC MARKET\n2026-10-25\nMilk $4.00\nTOTAL $4.00']}
    if cache_state == 'wrong_source':
        payload['metadata']['source_sha256'] = 'b' * 64
        legacy = {**payload, 'metadata': {**payload['metadata'], 'source_sha256': digest}}
        (cache / f'{digest}.json').write_text(json.dumps(legacy))
    if cache_state == 'missing_run':
        payload['metadata'].pop('run_uuid')
    if cache_state != 'missing':
        cache_path.write_text('{' if cache_state == 'invalid' else json.dumps(payload))
    original = cache_path.read_bytes() if cache_path.exists() else None

    def unexpected_ocr(*a, **kw):
        pytest.fail('Cached extraction must never execute OCR')
    monkeypatch.setattr(ingest, 'extract_page_text', unexpected_ocr)
    published = []
    connection = SimpleNamespace(channel=lambda: object(), close=lambda: None, is_open=True)
    monkeypatch.setattr(queue_ingest, 'connect_broker', lambda _: connection)
    monkeypatch.setattr(ocr_collector, 'declare_result_topology', lambda *a: None)
    monkeypatch.setattr(ocr_collector, 'publish_result', lambda channel, exchange, event: published.append(event))
    request_id = '22222222-2222-4222-8222-222222222222'
    message = ReceiptMessage(digest, source.name, version=2, request_id=request_id)
    args = SimpleNamespace(receipt_root=str(tmp_path), ocr_cache=str(cache), rabbitmq_url='test')
    if cache_state == 'valid':
        assert queue_ingest.publish_cached_reprocess(message, args) == 0
        event, = published
        assert event.payload['receipt']['merchant'] == 'SYNTHETIC MARKET'
        assert event.payload['persistence_mode'] == 'reprocess'
        assert event.payload['request_id'] == request_id
        assert event.run_uuid == payload['metadata']['run_uuid']
        assert event.artifacts[0].sha256 == ingest.sha256_file(cache_path)
    else:
        with pytest.raises(ValueError):
            queue_ingest.publish_cached_reprocess(message, args)
        assert not published
    assert (cache_path.read_bytes() if cache_path.exists() else None) == original


def test_cached_reextraction_keeps_review_status(tmp_path):
    source = tmp_path / 'receipt.pdf'
    source.write_bytes(b'evidence')
    cache = tmp_path / 'cache'
    cache.mkdir()
    (cache / 'receipt.pdf.json').write_text(json.dumps({
        'metadata': {'source_sha256': ingest.sha256_file(source), 'cache_version': 1},
        'page_text': ['SYNTHETIC MARKET\n2026-10-25\nMilk $4.00\nSUBTOTAL $9.00\nTOTAL $9.00'],
    }))
    receipt, hit = parallel_ingest._parse_scan_cached(source, tmp_path, cache, False, require_cache=True)
    assert hit and receipt.extraction_status == 'review'
    assert 'item_total_mismatch' in receipt.review_reasons
