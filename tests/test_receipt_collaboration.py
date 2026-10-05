from dataclasses import replace
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest

from home_budget_pipeline import receipt_collaboration as collab
from home_budget_pipeline import receipt_model_providers as providers
from home_budget_pipeline import receipt_shared_evidence as shared
from home_budget_pipeline import product_enrichment as core


URL = 'https://sobeys.com/products/old-el-paso-salsa-picante-medium'
TITLE = 'Old El Paso Salsa Picante Medium'
SOURCE = 'pass:run:1:line:1'


def bundle():
    return {'sources': [{'id': 'item:canonical', 'kind': 'canonical_item', 'text': 'Cep Pic Med'},
        {'id': SOURCE, 'kind': 'ocr_pass', 'engine': 'tesseract', 'text': 'Oep Pic Med'}],
        'documents': [], 'geometry': [], 'receipt_totals': {}, 'ocr_passes': [],
        'validations': {'receipt_arithmetic': 'pass', 'item_arithmetic': 'not_checkable'},
        'coverage': {'available_passes': 1, 'included_passes': 1, 'available_documents': 0, 'included_documents': 0}}


def review(provider, identifier=None, refs=None):
    return {'status': 'success', 'provider': provider, 'output': {
        'candidate_id': identifier or collab.candidate_id(URL),
        'source_ids': refs if refs is not None else [collab.candidate_id(URL), SOURCE], 'reason': 'supported'}}


def candidate():
    return {'id': collab.candidate_id(URL), 'url': URL, 'confidence': .9167,
            'evidence': {'product_page': True}, 'ocr_support': [SOURCE]}


def test_agreement_needs_two_provider_families_and_receipt_evidence():
    decision = collab.reconcile([candidate()], [review('openai'), review('qwen')], {}, .85, [], True)
    assert decision['disposition'] == 'recommended' and decision['canonical_updated'] is False
    decision = collab.reconcile([candidate()], [review('qwen'), review('qwen')], {}, .85, [], True)
    assert decision['disposition'] == 'review'
    assert 'insufficient_independent_provider_families' in decision['reasons']


@pytest.mark.parametrize('change,reason', [
    ('math', 'receipt_arithmetic_requires_review'),
    ('error', 'incomplete_provider_or_search_results'),
    ('context', 'evidence_context_truncated'),
    ('citations', 'review_missing_product_and_ocr_citations'),
    ('low_score', 'insufficient_original_receipt_product_evidence'),
    ('no_ocr', 'missing_supporting_ocr_observation'),
    ('disagreement', 'disagreement_or_abstention'),
])
def test_unsupported_agreement_stays_in_review(change, reason):
    c, reviews, checks, errors, complete = candidate(), [review('openai'), review('qwen')], {}, [], True
    if change == 'math': checks['receipt_arithmetic'] = 'fail'
    if change == 'error': errors.append({'stage': 'baseline', 'error_type': 'URLError'})
    if change == 'context': complete = False
    if change == 'citations': reviews[0]['output']['source_ids'] = [c['id']]
    if change == 'low_score': c['confidence'] = .7
    if change == 'no_ocr': c['ocr_support'] = []
    if change == 'disagreement': reviews[0]['output']['candidate_id'] = None
    result = collab.reconcile([c], reviews, checks, .85, errors, complete)
    assert result['disposition'] == 'review' and reason in result['reasons']


def test_unknown_citations_and_candidates_are_rejected():
    proposal = {'reading': 'Old El Paso', 'queries': ['Old El Paso Salsa'], 'source_ids': ['invented'], 'reason': 'claim'}
    with pytest.raises(ValueError): collab.validate_output(proposal, 'proposal', {SOURCE}, set())
    with pytest.raises(ValueError): collab.validate_output(review('openai')['output'], 'review', {SOURCE}, set())


def test_conflicting_lines_in_same_pass_remain_separate_search_hypotheses():
    sources = bundle()['sources'] + [
        {'id': 'pass:run:10:line:3', 'text': 'Cep Pic Med'},
        {'id': 'pass:run:10:line:2', 'text': 'Oep Pic Med'},
        {'id': 'pass:run:12:line:4', 'text': 'Oep Pic Med 6.49 C'}]
    hypotheses = collab.reading_hypotheses(sources, 'Sobeys')
    assert len(hypotheses) == 2
    assert hypotheses[1]['query'] == 'site:sobeys.com Oep Pic Med'
    assert hypotheses[1]['source_ids'] == [SOURCE, 'pass:run:10:line:2', 'pass:run:12:line:4']


@pytest.mark.parametrize('candidates', [set(), {collab.candidate_id(URL)}])
def test_review_schema_prevents_receipt_id_becoming_product(monkeypatch, candidates):
    def request(profile, text, schema, images):
        assert schema['properties']['candidate_id']['enum'] == [None, *sorted(candidates)]
        assert 'item:canonical' not in schema['properties']['candidate_id']['enum']
        return {'output': {'candidate_id': None, 'source_ids': [SOURCE], 'reason': 'No verified product'}}
    monkeypatch.setattr(providers, 'request', request)
    result = collab.call(providers.Profile('openai', 'openai', 'test'), 'review', '', {SOURCE}, candidates, [], {})
    assert result['status'] == 'success'
    assert 'enum' not in collab.REVIEW_SCHEMA['properties']['candidate_id']


def test_packing_preserves_duplicate_observations_and_reports_omissions():
    sources = [{'id': str(i), 'kind': 'ocr_pass', 'engine': engine, 'text': text}
               for i, (engine, text) in enumerate([('tesseract', 'A'), ('tesseract', 'A'), ('paddle', 'B')])]
    packed, coverage = collab.pack_sources(sources, 2000)
    assert coverage['included_sources'] == 3 and len(packed) == 2
    assert len(packed[0]['observations']) == 2
    _, coverage = collab.pack_sources(sources, 100)
    assert coverage['included_sources'] < 3


def test_model_prompt_retains_all_citations_without_repeating_artifact_metadata():
    sources = [{'id': f'pass:00000000-0000-0000-0000-000000000000:{i}:line:1',
                'kind': 'ocr_pass', 'engine': 'tesseract', 'text': 'Oep Pic Med',
                'ocr_run_uuid': '00000000-0000-0000-0000-000000000000',
                'text_artifact': {'uri': 'pvc://home-budget/receipts/derived/ocr-cache/receipt.pdf.json',
                                  'sha256': 'a' * 64}, 'quality': {'irrelevant': 'x' * 1000}}
               for i in range(41)]
    packed, coverage = collab.pack_sources(sources, 12480)
    assert coverage['included_sources'] == 41
    assert len(packed) == 1
    assert {o['id'] for o in packed[0]['observations']} == {s['id'] for s in sources}
    assert 'text_artifact' not in json.dumps(packed)
    assert coverage['text_characters'] < 7000
    assert sources[0]['text_artifact']['sha256'] == 'a' * 64


def test_image_context_reservation_rejects_prompt_before_model_request(monkeypatch):
    monkeypatch.setattr(providers, 'request', lambda *args: pytest.fail('Prompt exceeds reserved image budget'))
    profile = providers.Profile('openai', 'openai', 'test', context_tokens=8192, output_tokens=2048, vision=True)
    result = collab.call(profile, 'proposal', 'x' * 5000, set(), set(), [{'id': 'image:1:1'}], {})
    assert result['error_type'] == 'ContextBudgetError'
    assert result['reserved_image_tokens'] == 4096


def test_two_rounds_share_ocr_then_pool_all_provider_queries(monkeypatch, capsys):
    row = {'item_name': 'Cep Pic Med', 'store_name': 'Sobeys'}
    context = {'run_uuid': 'test-run', 'item_id': 1, 'receipt': 'a.pdf'}
    calls, searches = [], []
    result = core.SearchResult(TITLE, URL, '', .9167)
    def search(key, query, item, domain):
        searches.append(query)
        return [result]
    def request(profile, text, schema, images):
        calls.append((profile.provider, text))
        if schema == collab.PROPOSAL_SCHEMA:
            output = {'reading': TITLE, 'queries': [profile.provider + ' salsa'], 'source_ids': [SOURCE], 'reason': 'reading'}
        else:
            assert 'openai salsa' in text and 'anthropic salsa' in text
            output = review(profile.provider)['output']
        return {'output': output, 'input_tokens': 100, 'output_tokens': 20}
    monkeypatch.setattr(core, 'brave_candidates', search)
    monkeypatch.setattr(providers, 'request', request)
    profiles = [providers.Profile('openai', 'openai', 'test'), providers.Profile('anthropic', 'anthropic', 'test')]
    payload = collab.collaborate_item(row, bundle(), profiles, 'secret', .85, context)
    assert payload['decision']['disposition'] == 'recommended'
    assert [p for p, _ in calls] == ['openai', 'anthropic', 'openai', 'anthropic']
    assert all('Oep Pic Med' in text for _, text in calls)
    assert len(searches) == 4
    assert len(payload['proposals']) == len(payload['reviews']) == 2
    assert 'secret' not in capsys.readouterr().out


def test_alternative_search_precedes_models_even_when_models_favor_canonical(monkeypatch):
    evidence = bundle()
    evidence['sources'].append({'id': 'pass:run:2:line:1', 'kind': 'ocr_pass',
                                'engine': 'tesseract', 'text': 'Jep Pic Med'})
    searches = []
    def search(key, query, item, domain):
        searches.append(query)
        assert item == 'Cep Pic Med'  # Alternative discovery cannot inflate scoring.
        return []
    def request(profile, text, schema, images):
        assert 'site:sobeys.com Oep Pic Med' in searches
        assert 'site:sobeys.com Jep Pic Med' in searches
        assert 'reading_hypotheses' in text
        if schema == collab.PROPOSAL_SCHEMA:
            output = {'reading': 'Cep Pic Med', 'queries': ['Cepacol'],
                      'source_ids': ['item:canonical'], 'reason': 'Uncertain'}
        else:
            output = {'candidate_id': None, 'source_ids': [SOURCE], 'reason': 'No verified product'}
        return {'output': output}
    monkeypatch.setattr(core, 'brave_candidates', search)
    monkeypatch.setattr(providers, 'request', request)
    payload = collab.collaborate_item({'item_name': 'Cep Pic Med', 'store_name': 'Sobeys'},
        evidence, [providers.Profile('openai', 'openai', 'test')], 'key', .85, {'receipt': 'a.pdf'})
    assert searches.count('site:sobeys.com Oep Pic Med') == 1
    assert payload['decision']['disposition'] == 'review'
    assert payload['reading_hypotheses'][1]['source_ids'] == [SOURCE]


def test_failed_provider_does_not_stop_other_workers(monkeypatch):
    monkeypatch.setattr(core, 'brave_candidates', lambda *a: [core.SearchResult(TITLE, URL, '', .9167)])
    def request(profile, text, schema, images):
        if profile.provider == 'anthropic': raise OSError('credential secret')
        output = {'reading': TITLE, 'queries': ['Salsa'], 'source_ids': [SOURCE], 'reason': 'ok'} if schema == collab.PROPOSAL_SCHEMA else review('openai')['output']
        return {'output': output}
    monkeypatch.setattr(providers, 'request', request)
    profiles = [providers.Profile('openai', 'openai', 'test'), providers.Profile('anthropic', 'anthropic', 'test')]
    payload = collab.collaborate_item({'item_name': 'Cep Pic Med', 'store_name': 'Sobeys'}, bundle(), profiles,
                                    'key', .85, {'receipt': 'a.pdf'})
    assert payload['decision']['disposition'] == 'review' and len(payload['reviews']) == 2
    assert len(payload['errors']) == 2
    assert 'credential secret' not in json.dumps(payload)


def test_loader_uses_latest_passes_and_decimal_arithmetic():
    statements = []
    class Connection:
        def execute(self, sql, params): statements.append(sql); return self
        def fetchall(self):
            if 'SELECT id,' in statements[-1]: return [{'id': 1, 'source_reference': 'a.pdf', 'source_sha256': 'a' * 64, 'raw_text': 'Oep Pic Med 3.49'}]
            if 'SELECT p.*' in statements[-1]: return [{
                'run_uuid': 'run', 'pass_id': 2, 'available_passes': 1, 'status': 'success',
                'extracted_text': 'Oep Pic Med 3.49', 'seconds': Decimal('1.2'), 'engine': 'paddle',
                'engine_type': 'traditional_ocr', 'evidence_id': 1, 'page_number': 1, 'variant': 'raw'}]
            return []
    row = {'item_name': 'Cep Pic Med', 'store_name': 'Sobeys', 'receipt_id': 1,
           'unit_qty': Decimal('1'), 'unit_cost': Decimal('3.49'), 'line_total': Decimal('3.49'),
           'expense_total': Decimal('3.49'), 'receipt_item_subtotal': Decimal('3.49'), 'total_recon_diff': Decimal('0')}
    result = shared.load(Connection(), row)
    assert any(s.get('engine') == 'paddle' for s in result['sources'])
    assert result['validations'] == {'receipt_arithmetic': 'pass', 'item_arithmetic': 'pass'}
    assert 'latest.processed_at DESC' in statements[1]


def test_images_require_safe_path_and_matching_source_hash(monkeypatch, tmp_path):
    monkeypatch.setenv('HOME_BUDGET_RECEIPTS_ROOT', str(tmp_path))
    data = bundle()
    data['documents'] = [{'id': 1, 'source_reference': '../outside.png', 'source_sha256': 'a' * 64}]
    images, errors = shared.render_images(data)
    assert not images and errors[0]['reason'] == 'outside_receipt_root'
    (tmp_path / 'a.png').write_bytes(b'not matching evidence')
    data['documents'][0]['source_reference'] = 'a.png'
    images, errors = shared.render_images(data)
    assert not images and errors[0]['reason'] == 'source_hash_mismatch'


def test_verified_image_is_bounded_and_identified(monkeypatch, tmp_path):
    import hashlib
    from PIL import Image
    monkeypatch.setenv('HOME_BUDGET_RECEIPTS_ROOT', str(tmp_path))
    path = tmp_path / 'receipt.png'
    Image.new('RGB', (3000, 1000), 'white').save(path)
    data = bundle()
    data['documents'] = [{'id': 3, 'source_reference': 'receipt.png',
                         'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}]
    images, errors = shared.render_images(data)
    assert not errors and len(images) == 1
    assert images[0]['id'] == 'image:3:1' and len(images[0]['sha256']) == 64


def test_large_verified_pdf_is_rendered_from_stream(monkeypatch, tmp_path):
    import hashlib
    import pypdfium2
    from PIL import Image
    monkeypatch.setenv('HOME_BUDGET_RECEIPTS_ROOT', str(tmp_path))
    path = tmp_path / 'scan.pdf'
    Image.new('RGB', (300, 600), 'white').save(path, format='PDF')
    with path.open('ab') as source:
        for _ in range(21):
            source.write(b'\n' * 1024 ** 2)
    assert path.stat().st_size > 20 * 1024 ** 2
    with path.open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
    real_document = pypdfium2.PdfDocument
    def document(source):
        assert hasattr(source, 'read') and hasattr(source, 'seek')
        return real_document(source)
    monkeypatch.setattr(pypdfium2, 'PdfDocument', document)
    data = bundle()
    data['documents'] = [{'id': 3, 'source_reference': 'scan.pdf', 'source_sha256': digest}]
    images, errors = shared.render_images(data)
    assert not errors and len(images) == 1
    assert images[0]['id'] == 'image:3:1'
    monkeypatch.setattr(shared, 'MAX_SOURCE_BYTES', 20 * 1024 ** 2)
    images, errors = shared.render_images(data)
    assert not images and errors[0]['reason'] == 'source_too_large'


def test_collaboration_persists_only_evidence_and_run_summary(monkeypatch, capsys):
    import psycopg
    calls = []
    row = {'id': 42, 'receipt_id': 7, 'item_name': 'Cep Pic Med', 'store_name': 'Sobeys',
           'receipt_filename': 'receipt.pdf', 'source_reference': None, 'receipt_item_count': 3}
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def commit(self): pass
        def execute(self, sql, params): calls.append((sql, params)); return self
        def fetchall(self): return [row]
    monkeypatch.setattr(psycopg, 'connect', lambda *a, **kw: Connection())
    monkeypatch.setattr(providers, 'profiles', lambda: ([providers.Profile('test', 'openai', 'test')], []))
    monkeypatch.setattr(shared, 'load', lambda *a: bundle())
    monkeypatch.setattr(collab, 'collaborate_item', lambda *a: {
        'decision': {'disposition': 'review'}, 'errors': [], 'item_seconds': 2.5})
    stats = collab.run(dsn='unused', api_key='unused', limit=1, threshold=.85, write_db=True)
    mutations = [sql for sql, _ in calls if not sql.lstrip().startswith('SELECT')]
    assert len(mutations) == 2 and all('INSERT INTO enrichment.receipt_collaboration' in sql for sql in mutations)
    assert stats['review'] == 1 and stats['receipts'][0]['complete_receipt'] is False
    assert stats['receipts'][0]['enrichment_seconds'] == 2.5
    assert 'enrichment_receipt_timing' in capsys.readouterr().out


def test_bad_citations_retain_usage_and_output_for_review(monkeypatch):
    monkeypatch.setattr(providers, 'request', lambda *a: {'input_tokens': 200, 'output_tokens': 40,
        'output': {'reading': 'Salsa', 'queries': ['Salsa'], 'source_ids': ['invented'], 'reason': 'unsupported'}})
    result = collab.call(providers.Profile('test', 'openai', 'test'), 'proposal', 'evidence',
                         {SOURCE}, set(), [], {'receipt': 'a.pdf'})
    assert result['status'] == 'error' and result['error_type'] == 'EvidenceCitationError'
    assert result['input_tokens'] == 200 and result['output_tokens'] == 40
    assert result['invalid_output']['source_ids'] == ['invented']
