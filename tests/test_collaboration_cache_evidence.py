import hashlib
import json

import pytest

from home_budget_pipeline import receipt_shared_evidence as shared


def cache(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME_BUDGET_OCR_CACHE', str(tmp_path))
    monkeypatch.setenv('OCR_ARTIFACT_URI_PREFIX', 'pvc://receipt-ocr')
    document = {'id': 1, 'source_reference': 'receipt.pdf', 'source_sha256': 'a' * 64}
    metric = {'run_uuid': 'run', 'pass_id': 10, 'engine': 'tesseract',
              'status': 'success', 'page_number': 1, 'variant': 'enhanced',
              'evidence_id': 1, 'extracted_text': None}
    payload = {'metadata': {'run_uuid': 'run', **{k: document[k] for k in ('source_reference', 'source_sha256')},
                            'ocr_passes': [{**metric, 'text': 'Oep Pic Med'}]}}
    data = json.dumps(payload).encode()
    (tmp_path / 'cache.json').write_bytes(data)
    artifact = {'run_uuid': 'run', 'uri': 'pvc://receipt-ocr/cache.json',
                'sha256': hashlib.sha256(data).hexdigest(), 'size_bytes': len(data)}
    return artifact, document, metric, payload


def test_current_verified_cache_recovers_ocr_alternative_with_provenance(tmp_path, monkeypatch):
    artifact, document, metric, _ = cache(tmp_path, monkeypatch)
    class Connection:
        def execute(self, sql, params):
            assert "kind='ocr-cache'" in sql and params == (['run'],)
            return self
        def fetchall(self):
            return [artifact]
    assert shared.recover_pass_text(Connection(), [document], [metric]) == []
    assert metric['extracted_text'] == 'Oep Pic Med'
    assert metric['text_artifact']['sha256'] == artifact['sha256']
    assert shared.relevant_lines(metric['extracted_text'], 'Cep Pic Med', 'sobeys.com')[0]['text'] == 'Oep Pic Med'


@pytest.mark.parametrize('change,reason', [
    ({'uri': 'pvc://receipt-ocr/../cache.json'}, 'outside_cache_root'),
    ({'uri': 'https://example.com/cache.json'}, 'unsupported_artifact_uri'),
    ({'size_bytes': 1}, 'cache_size_mismatch'),
    ({'sha256': 'b' * 64}, 'cache_hash_mismatch'),
    ({'run_uuid': 'other'}, 'cache_run_mismatch'),
])
def test_invalid_reference_is_rejected(tmp_path, monkeypatch, change, reason):
    artifact, document, metric, _ = cache(tmp_path, monkeypatch)
    with pytest.raises(shared.CacheEvidenceError, match=reason):
        shared.verified_pass_text({**artifact, **change}, document, [metric])


@pytest.mark.parametrize('field,value,reason', [
    ('source_sha256', 'b' * 64, 'cache_source_mismatch'),
    ('source_reference', 'other.pdf', 'cache_source_mismatch'),
    ('ocr_passes', [], 'cache_pass_mismatch'),
])
def test_correct_hash_cannot_substitute_wrong_source_or_pass(tmp_path, monkeypatch, field, value, reason):
    artifact, document, metric, payload = cache(tmp_path, monkeypatch)
    payload['metadata'][field] = value
    data = json.dumps(payload).encode()
    (tmp_path / 'cache.json').write_bytes(data)
    artifact.update(sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data))
    with pytest.raises(shared.CacheEvidenceError, match=reason):
        shared.verified_pass_text(artifact, document, [metric])


def test_missing_or_changed_cache_is_reported_without_stale_fallback(tmp_path, monkeypatch):
    artifact, document, metric, _ = cache(tmp_path, monkeypatch)
    (tmp_path / 'cache.json').write_text('changed')
    class Connection:
        def execute(self, sql, params): return self
        def fetchall(self): return [artifact]
    errors = shared.recover_pass_text(Connection(), [document], [metric])
    assert errors[0]['reason'] == 'cache_size_mismatch'
    assert metric['extracted_text'] is None
    class Empty(Connection):
        def fetchall(self): return []
    assert shared.recover_pass_text(Empty(), [document], [metric])[0]['reason'] == 'cache_artifact_missing'


def test_persisted_text_never_requires_or_gets_replaced_by_cache(tmp_path, monkeypatch):
    _, document, metric, _ = cache(tmp_path, monkeypatch)
    metric['extracted_text'] = 'Stored pass text'
    class Connection:
        def execute(self, *args): raise AssertionError('No cache lookup needed')
    assert shared.recover_pass_text(Connection(), [document], [metric]) == []
    assert metric['extracted_text'] == 'Stored pass text'
