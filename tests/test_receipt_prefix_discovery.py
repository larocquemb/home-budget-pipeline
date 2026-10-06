"""Regression coverage for the lexical task verified against live Qwen."""
import json
import pytest

from home_budget_pipeline import receipt_collaboration as c
from home_budget_pipeline import receipt_model_providers as p
from home_budget_pipeline import receipt_shared_evidence as shared
from home_budget_pipeline import product_enrichment as core

pytestmark = pytest.mark.usefixtures('offline_gpu_sampling')
URL = 'https://www.sobeys.com/products/old-el-paso-salsa-picante-style-restaurant-medium-650-ml'
TITLE = 'Buy Old El Paso Salsa Picante Style Restaurant Medium 650 ml | Sobeys Inc.'
SOURCE = 'pass:alternative:1'


def test_prefix_discovery_verifies_live_recorded_queries_before_review(monkeypatch):
    calls, queries = [], []
    def request(profile, text, schema, images):
        if set(schema['properties']) == {'queries'}:
            calls.append('prefix')
            assert images == [] and 'PIC MED' in text
            assert schema['properties']['queries']['items']['pattern'] == '^pic[a-z]+ med[a-z]+$'
            return {'output': {'queries': ['picante medium', 'pictorial medium', 'piccolo medium']}}
        calls.append('review')
        assert images and TITLE in text
        assert SOURCE in schema['properties']['source_ids']['items']['enum']
        identifier = c.candidate_id(URL)
        return {'output': {'candidate_id': identifier, 'candidate_title': TITLE,
            'product_source_id': identifier, 'source_ids': [SOURCE], 'reason': 'Retailer product and alternative OCR agree'}}
    def search(key, query, item, domain):
        queries.append(query)
        assert item == 'Cep Pic Med' and domain == 'sobeys.com'
        return [core.SearchResult(TITLE, URL, '', .9167)] if query == 'site:sobeys.com picante medium' else []
    evidence = {'sources': [{'id': 'canonical', 'kind': 'canonical_item', 'text': 'Cep Pic Med'},
        {'id': SOURCE, 'kind': 'ocr_pass', 'engine': 'tesseract', 'text': 'Oep Pic Med'}],
        'documents': [], 'receipt_totals': {}, 'coverage': {'available_passes': 1, 'included_passes': 1,
        'available_documents': 0, 'included_documents': 0},
        'validations': {'receipt_arithmetic': 'pass', 'item_arithmetic': 'pass'}}
    monkeypatch.setattr(p, 'request', request)
    monkeypatch.setattr(core, 'brave_candidates', search)
    monkeypatch.setattr(shared, 'render_images', lambda _: ([{'id': 'image:1', 'data': 'actual-image-fixture'}], []))
    result = c.collaborate_item({'item_name': 'Cep Pic Med', 'store_name': 'Sobeys'}, evidence,
        [p.Profile('qwen', 'qwen', 'qwen3-vl-receipts:30b-instruct', vision=True)], 'key', .85, {'receipt': 'a.pdf'})
    assert calls == ['prefix', 'review']
    assert result['decision']['disposition'] == 'recommended'
    assert result['decision']['confidence'] == .9167
    assert result['decision']['canonical_updated'] is False
    assert result['errors'] == []
    assert result['discovery']['expansion_calls'] == 0
    assert result['discovery']['descriptive_expansion_calls'] == 1
    assert 'site:sobeys.com pictorial medium' not in queries


@pytest.mark.parametrize('query', ['pic med', 'plain rice pasta medium', 'cep picante medium', 'piquant medium'])
def test_prefix_schema_is_revalidated_locally(monkeypatch, query):
    monkeypatch.setattr(p, 'request', lambda *args: {'output': {'queries': [query] * 3}})
    result = c.expand_descriptive_prefixes([{'reading': 'ep Pic Med', 'source_ids': [SOURCE]}],
        [p.Profile('qwen', 'qwen', 'local')], {})[0]
    assert result['status'] == 'error' and result['error_type'] == 'ValueError'
    assert 'output' not in result


@pytest.mark.parametrize('stage', ['proposal', 'expansion'])
def test_discovery_schema_cites_only_supplied_ids(monkeypatch, stage):
    def request(profile, text, schema, images):
        assert schema['properties']['source_ids']['items']['enum'] == [SOURCE]
        assert schema['properties']['source_ids']['maxItems'] == 6
        return {'output': {'reading': 'hypothesis', 'queries': ['picante medium'],
            'source_ids': ['pass:omitted'], 'reason': 'invalid citation'}}
    monkeypatch.setattr(p, 'request', request)
    result = c.call(p.Profile('qwen', 'qwen', 'local'), stage, 'evidence', {SOURCE}, set(), [], {})
    assert result['status'] == 'error' and result['error_type'] == 'EvidenceCitationError'
