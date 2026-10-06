from dataclasses import replace
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest

from home_budget_pipeline import receipt_collaboration as collab
from home_budget_pipeline import receipt_model_providers as providers
from home_budget_pipeline import receipt_shared_evidence as shared
from home_budget_pipeline import product_enrichment as core

pytestmark = pytest.mark.usefixtures('offline_gpu_sampling')


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
    return {'status': 'success', 'provider': provider, 'profile': provider, 'output': {
        'candidate_id': identifier or collab.candidate_id(URL),
        'candidate_title': TITLE,
        'product_source_id': identifier or collab.candidate_id(URL),
        'source_ids': refs if refs is not None else [collab.candidate_id(URL), SOURCE], 'reason': 'supported'}}


def candidate():
    return {'id': collab.candidate_id(URL), 'title': TITLE, 'url': URL, 'confidence': .9167,
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
        assert schema['properties']['product_source_id']['enum'] == [None, *sorted(candidates)]
        assert 'product_source_id' in schema['required']
        return {'output': {'candidate_id': None, 'candidate_title': None, 'product_source_id': None, 'source_ids': [SOURCE], 'reason': 'No verified product'}}
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
    assert len(searches) == 6
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
        if schema == collab.EXPANSION_SCHEMA:
            target = json.loads(next(line for line in text.splitlines() if line.startswith('{')))['target_reading']
            return {'output': {'reading': target['reading'], 'queries': [],
                              'source_ids': target['source_ids'][:1], 'reason': 'No defensible expansion'}}
        assert 'reading_hypotheses' in text or 'expansion_hypotheses' in text
        if schema == collab.PROPOSAL_SCHEMA:
            output = {'reading': 'Cep Pic Med', 'queries': ['Cepacol'],
                      'source_ids': ['item:canonical'], 'reason': 'Uncertain'}
        else:
            output = {'candidate_id': None, 'candidate_title': None, 'product_source_id': None, 'source_ids': [SOURCE], 'reason': 'No verified product'}
        return {'output': output}
    monkeypatch.setattr(core, 'brave_candidates', search)
    monkeypatch.setattr(providers, 'request', request)
    payload = collab.collaborate_item({'item_name': 'Cep Pic Med', 'store_name': 'Sobeys'},
        evidence, [providers.Profile('openai', 'openai', 'test')], 'key', .85, {'receipt': 'a.pdf'})
    assert searches.count('site:sobeys.com Oep Pic Med') == 1
    assert payload['decision']['disposition'] == 'review'
    assert payload['reading_hypotheses'][1]['source_ids'] == [SOURCE]


def test_isolated_expansions_retry_unexpanded_tokens_then_search_full_product(monkeypatch):
    searches, targets, attempts = [], [], {}
    def search(key, query, item, domain):
        searches.append(query)
        assert item == 'Cep Pic Med'
        return [core.SearchResult(TITLE, URL, '', .9167)] if 'Old El Paso' in query else []
    def request(profile, text, schema, images):
        if schema == collab.EXPANSION_SCHEMA:
            target = json.loads(next(line for line in text.splitlines() if line.startswith('{')))['target_reading']
            reading = target['reading']
            assert images == []
            assert ('Cep Pic Med' not in text) if reading == 'Oep Pic Med' else ('Oep Pic Med' not in text)
            targets.append((profile.provider, reading))
            key = (profile.provider, reading)
            attempts[key] = attempts.get(key, 0) + 1
            queries = ['Cepacol Pic Med'] if attempts[key] == 1 else ['Old El Paso medium picante salsa']
            output = {'reading': TITLE, 'queries': queries, 'source_ids': target['source_ids'][:1], 'reason': 'Unverified expansion'}
        elif schema == collab.PROPOSAL_SCHEMA:
            output = {'reading': 'Cep Pic Med', 'queries': ['Cepacol'], 'source_ids': ['item:canonical'], 'reason': 'Uncertain'}
        else:
            assert TITLE in text
            output = review(profile.provider)['output']
        return {'output': output, 'input_tokens': 100, 'output_tokens': 50}
    monkeypatch.setattr(core, 'brave_candidates', search)
    monkeypatch.setattr(providers, 'request', request)
    profiles = [providers.Profile('openai', 'openai', 'test'), providers.Profile('qwen', 'qwen', 'test')]
    collab.expand_readings(collab.reading_hypotheses(bundle()['sources'], 'Sobeys'), profiles, 'Sobeys', {})
    assert set(targets) == {(p, r) for p in ('openai', 'qwen') for r in ('Cep Pic Med', 'Oep Pic Med')}
    targets.clear()
    attempts.clear()
    payload = collab.collaborate_item({'item_name': 'Cep Pic Med', 'store_name': 'Sobeys'},
        bundle(), profiles, 'key', .85, {'receipt': 'a.pdf'})
    assert set(targets) == {('qwen', 'Cep Pic Med'), ('qwen', 'Oep Pic Med'), ('openai', 'Cep Pic Med')}
    assert attempts[('qwen', 'Cep Pic Med')] == attempts[('qwen', 'Oep Pic Med')] == 1
    assert attempts[('openai', 'Cep Pic Med')] == 2
    assert searches.count('site:sobeys.com Old El Paso medium picante salsa') == 1
    assert all('Pic Med' not in q for e in payload['expansions'] for q in e['output']['queries'])
    assert payload['decision']['disposition'] == 'recommended'
    assert payload['decision']['canonical_updated'] is False
    assert payload['proposals'] == []
    assert {r['provider'] for r in payload['reviews']} == {'openai', 'qwen'}


def test_expansion_cannot_cite_competing_reading_and_can_abstain(monkeypatch):
    hypothesis = {'reading': 'Oep Pic Med', 'query': 'site:sobeys.com Oep Pic Med', 'source_ids': [SOURCE]}
    profile = providers.Profile('openai', 'openai', 'test')
    monkeypatch.setattr(providers, 'request', lambda *a: {'output': {
        'reading': TITLE, 'queries': [TITLE], 'source_ids': ['item:canonical'], 'reason': 'Wrong citation'}})
    result = collab.expand_readings([hypothesis], [profile], 'Sobeys', {})[0]
    assert result['status'] == 'error' and result['error_type'] == 'EvidenceCitationError'
    monkeypatch.setattr(providers, 'request', lambda *a: {'output': {
        'reading': 'Oep Pic Med', 'queries': [], 'source_ids': [SOURCE], 'reason': 'Cannot infer'}})
    result = collab.expand_readings([hypothesis], [profile], 'Sobeys', {})[0]
    assert result['status'] == 'success' and result['expansion_state'] == 'unresolved'
    assert len(result['attempts']) == 1


def test_truncated_expansion_retries_once_and_preserves_first_usage(monkeypatch):
    calls = []
    def request(profile, text, schema, images):
        calls.append(text)
        if len(calls) == 1:
            raise providers.IncompleteModelOutput({'output_tokens': 2048, 'finish_reason': 'length'})
        assert 'response was truncated' in text
        return {'output': {'reading': TITLE, 'queries': [TITLE], 'source_ids': [SOURCE], 'reason': 'Hypothesis'}}
    monkeypatch.setattr(providers, 'request', request)
    result = collab.expand_readings([{'reading': 'Oep Pic Med', 'source_ids': [SOURCE]}],
        [providers.Profile('openai', 'openai', 'test')], 'Sobeys', {})[0]
    assert result['status'] == 'success' and len(result['attempts']) == 2
    assert result['attempts'][0]['output_tokens'] == 2048


@pytest.mark.parametrize('refs', [[SOURCE], [collab.candidate_id(URL)]])
@pytest.mark.parametrize('repaired', [True, False])
def test_review_requires_model_to_reissue_complete_product_and_ocr_citations(monkeypatch, refs, repaired):
    calls = []
    def request(profile, text, schema, images):
        calls.append(text)
        if len(calls) == 2:
            assert 'previous review lacked valid evidence citations' in text
        output = review('openai', refs=refs if len(calls) == 1 or not repaired else None)['output']
        if refs == [SOURCE] and (len(calls) == 1 or not repaired):
            output['product_source_id'] = None
        return {'output': output, 'output_tokens': 50}
    monkeypatch.setattr(providers, 'request', request)
    result = collab.review_with_citations(providers.Profile('openai', 'openai', 'test'), 'evidence',
        {SOURCE, collab.candidate_id(URL)}, [candidate()], [], {})
    assert len(calls) == 2
    assert result['status'] == ('success' if repaired else 'error')
    assert result['attempts'][0]['invalid_output']['source_ids'] == refs
    if repaired:
        assert result['output']['source_ids'] == [collab.candidate_id(URL), SOURCE]


def test_brand_index_is_not_a_selectable_review_product(monkeypatch):
    monkeypatch.setattr(core, 'brave_candidates', lambda *a: [core.SearchResult(
        'Cepacol Products', 'https://sobeys.com/products/brand/Cepacol', '', 0)])
    def request(profile, text, schema, images):
        if schema == collab.EXPANSION_SCHEMA:
            h = json.loads(next(line for line in text.splitlines() if line.startswith('{')))['target_reading']
            output = {'reading': h['reading'], 'queries': [], 'source_ids': h['source_ids'][:1], 'reason': 'Unknown'}
        elif schema == collab.PROPOSAL_SCHEMA:
            output = {'reading': 'Cep Pic Med', 'queries': ['Cepacol'], 'source_ids': ['item:canonical'], 'reason': 'Unknown'}
        else:
            assert schema['properties']['candidate_id']['enum'] == [None]
            output = {'candidate_id': None, 'candidate_title': None, 'product_source_id': None, 'source_ids': [SOURCE], 'reason': 'Only a brand index found'}
        return {'output': output}
    monkeypatch.setattr(providers, 'request', request)
    payload = collab.collaborate_item({'item_name': 'Cep Pic Med', 'store_name': 'Sobeys'},
        bundle(), [providers.Profile('openai', 'openai', 'test')], 'key', .85, {'receipt': 'a.pdf'})
    assert payload['candidates'] == [] and len(payload['search_results']) == 1
    assert payload['decision']['disposition'] == 'review' and payload['errors'] == []


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
    monkeypatch.setattr(providers, 'profiles', lambda: ([providers.Profile('test', 'qwen', 'test')], []))
    monkeypatch.setattr(shared, 'load', lambda *a: bundle())
    monkeypatch.setattr(collab, 'collaborate_item', lambda *a: {
        'decision': {'disposition': 'review'}, 'errors': [{'stage': 'expansion'}], 'blocking_errors': [], 'item_seconds': 2.5})
    stats = collab.run(dsn='unused', api_key='unused', limit=1, threshold=.85, write_db=True)
    mutations = [sql for sql, _ in calls if not sql.lstrip().startswith('SELECT')]
    assert len(mutations) == 2 and all('INSERT INTO enrichment.receipt_collaboration' in sql for sql in mutations)
    assert stats['review'] == 1 and stats['incomplete'] == 0 and stats['receipts'][0]['complete_receipt'] is False
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


@pytest.mark.parametrize('issue', [None, 'expansion', 'citation', 'abstention', 'arithmetic', 'coverage', 'search', 'score', 'image'])
def test_qwen_first_skips_paid_models_only_with_complete_supported_evidence(monkeypatch, issue):
    data = bundle()
    data['validations']['item_arithmetic'] = 'pass'
    if issue == 'arithmetic': data['validations']['receipt_arithmetic'] = 'fail'
    if issue == 'coverage': data['coverage']['available_passes'] = 2
    calls = []
    if issue == 'image':
        monkeypatch.setattr(shared, 'render_images', lambda bundle: ([], [{'error_type': 'ImageError'}]))
    def search(key, query, item, domain):
        if issue == 'expansion' and (query.endswith('Pic Med') or query in {'site:sobeys.com pic', 'site:sobeys.com med'}): return []
        if issue == 'search' and query.endswith('Oep Pic Med'):
            raise OSError('unavailable')
        return [core.SearchResult(TITLE, URL, '', .9167)]
    monkeypatch.setattr(core, 'brave_candidates', search)
    if issue == 'score':
        original = core.candidate_evidence
        def score(*args, **kwargs):
            return {**original(*args, **kwargs), 'confidence': .7}
        monkeypatch.setattr(core, 'candidate_evidence', score)
    def request(profile, text, schema, images):
        calls.append((profile.provider, 'candidate_id' in schema['properties']))
        if 'candidate_id' in schema['properties']:
            output = review(profile.provider, refs=[SOURCE])['output']
            if profile.provider == 'qwen' and issue == 'citation': output['product_source_id'] = None
            if profile.provider == 'qwen' and issue == 'abstention':
                output.update(candidate_id=None, candidate_title=None, product_source_id=None)
        else:
            refs = json.loads(text.splitlines()[-1])['target_reading']['source_ids'] if schema == collab.EXPANSION_SCHEMA else [SOURCE]
            output = {'reading': TITLE, 'queries': [TITLE], 'source_ids': refs[:1], 'reason': 'hypothesis'}
        return {'output': output}
    monkeypatch.setattr(providers, 'request', request)
    profiles = [providers.Profile('openai', 'openai', 'test'), providers.Profile('qwen', 'qwen', 'test', vision=issue == 'image')]
    payload = collab.collaborate_item({'item_name': 'Cep Pic Med', 'store_name': 'Sobeys'},
        data, profiles, 'key', .85, {'receipt': 'a.pdf'})
    assert calls[0][0] == 'qwen'
    policy = payload['decision']['review_policy']
    if issue in (None, 'expansion'):
        assert all(provider == 'qwen' for provider, _ in calls)
        assert policy['skipped_profiles'] == ['openai']
        assert policy['fallback_reasons'] == []
        assert payload['decision']['disposition'] == 'recommended'
        assert payload['decision']['provider_families'] == ['qwen']
        assert payload['reviews'][0]['output']['source_ids'] == [SOURCE]
        assert bool(payload['expansions']) == (issue == 'expansion')
    else:
        assert ('openai', True) in calls
        assert policy['fallback_reasons'] and policy['skipped_profiles'] == []
        assert payload['decision']['disposition'] == ('recommended' if issue == 'image' else 'review')


def test_product_citation_must_match_selection_and_abstention():
    output = review('qwen', refs=[SOURCE])['output']
    assert collab.validate_output(output, 'review', {SOURCE}, {output['candidate_id']}) == output
    for invalid in ({**output, 'product_source_id': 'product:other'},
                    {**output, 'candidate_id': None}, {**output, 'product_source_id': None}):
        with pytest.raises(collab.EvidenceCitationError):
            collab.validate_output(invalid, 'review', {SOURCE}, {output['candidate_id']})


def test_historical_reviews_still_reconcile_with_product_in_source_ids():
    reviews = [review('qwen'), review('openai')]
    for result in reviews: result['output'].pop('product_source_id')
    assert collab.reconcile([candidate()], reviews, {}, .85, [], True)['disposition'] == 'recommended'


@pytest.mark.parametrize('recovered', [True, False])
def test_qwen_expansion_recovery_increases_budget_once_then_stops_profile(monkeypatch, recovered):
    calls = []
    def request(profile, text, schema, images):
        calls.append((profile.provider, profile.output_tokens))
        if profile.provider == 'qwen' and (len(calls) == 1 or not recovered):
            raise providers.IncompleteModelOutput({'output_tokens': profile.output_tokens, 'finish_reason': 'length'})
        target = json.loads(text.splitlines()[-1])['target_reading'] if 'truncated' not in text else {'source_ids': [SOURCE]}
        return {'output': {'reading': TITLE, 'queries': [TITLE], 'source_ids': target['source_ids'][:1], 'reason': 'hypothesis'}}
    monkeypatch.setattr(providers, 'request', request)
    profile = providers.Profile('qwen', 'qwen', 'test')
    hypotheses = [{'reading': reading, 'source_ids': [SOURCE]} for reading in ['Oep Pic Med', 'Cep Pic Med', 'Gep Pic Med']]
    results = collab.expand_readings(hypotheses, [profile, providers.Profile('openai', 'openai', 'test')],
        'Sobeys', {}, verify=lambda result: True)
    assert calls[:2] == [('qwen', 2048), ('qwen', 4096)]
    assert profile.output_tokens == 2048
    assert results[0]['attempts'][0]['finish_reason'] == 'length'
    if recovered:
        assert len(calls) == 2 and results[0]['status'] == 'success'
    else:
        assert calls == [('qwen', 2048), ('qwen', 4096), ('openai', 2048)]
        assert results[0]['stop_reason'] == 'provider_truncation_circuit_open'
        assert results[0]['status'] == 'error'
        assert results[1]['verified_search_match'] is True


@pytest.mark.parametrize('recovered', [True, False])
def test_qwen_review_retries_truncation_once_with_bounded_budget(monkeypatch, recovered):
    budgets = []
    def request(profile, text, schema, images):
        budgets.append(profile.output_tokens)
        if len(budgets) == 1 or not recovered:
            raise providers.IncompleteModelOutput({'output_tokens': profile.output_tokens, 'finish_reason': 'length'})
        assert 'response was truncated' in text
        return {'output': review('qwen', refs=[SOURCE])['output']}
    monkeypatch.setattr(providers, 'request', request)
    result = collab.review_with_citations(providers.Profile('qwen', 'qwen', 'test'), 'evidence',
        {SOURCE}, [candidate()], [], {})
    assert budgets == [2048, 4096]
    assert result['attempts'][1]['requested_output_tokens'] == 4096
    assert result['status'] == ('success' if recovered else 'error')
    assert result['attempts'][0]['finish_reason'] == 'length'
    if recovered: assert result['output']['source_ids'] == [SOURCE]


@pytest.mark.parametrize('quoted_has_match', [True, False])
def test_empty_quoted_expansion_search_broadens_once_and_keeps_evidence(monkeypatch, quoted_has_match):
    quoted = 'site:sobeys.com "Old El Paso" "Medium Picante"'
    broad = 'site:sobeys.com Old El Paso Medium Picante'
    searches = []
    def search(key, query, item, domain):
        searches.append(query)
        assert item == 'Cep Pic Med' and domain == 'sobeys.com'
        return [core.SearchResult(TITLE, URL, '', .9167)] if query == broad or (query == quoted and quoted_has_match) else []
    def request(profile, text, schema, images):
        assert profile.provider == 'qwen'
        if schema == collab.EXPANSION_SCHEMA:
            target = json.loads(text.splitlines()[-1])['target_reading']
            output = {'reading': TITLE, 'queries': [quoted], 'source_ids': target['source_ids'][:1], 'reason': 'hypothesis'}
        else: output = review('qwen', refs=[SOURCE])['output']
        return {'output': output}
    monkeypatch.setattr(core, 'brave_candidates', search)
    monkeypatch.setattr(providers, 'request', request)
    data = bundle()
    data['validations']['item_arithmetic'] = 'pass'
    payload = collab.collaborate_item({'item_name': 'Cep Pic Med', 'store_name': 'Sobeys'}, data,
        [providers.Profile('openai', 'openai', 'test'), providers.Profile('qwen', 'qwen', 'test')],
        'key', .85, {'receipt': 'a.pdf'})
    assert payload['decision']['disposition'] == 'recommended'
    assert payload['decision']['review_policy']['skipped_profiles'] == ['openai']
    assert searches.count(quoted) == 1 and searches.count(broad) == (0 if quoted_has_match else 1)
    expansion = payload['expansions'][0]
    if not quoted_has_match:
        assert expansion['searched_queries'] == [quoted, broad]
        assert expansion['query_fallbacks'] == [{'original_query': quoted, 'fallback_query': broad, 'reason': 'empty_quoted_results'}]
        recorded = {q['query']: q['candidate_ids'] for q in payload['search_queries']}
        assert recorded[quoted] == [] and recorded[broad] == [collab.candidate_id(URL)]


@pytest.mark.parametrize('issue', [None, 'search', 'citation', 'arithmetic', 'unknown_arithmetic', 'context', 'weak', 'title', 'unverified', 'profile'])
def test_discovery_truncation_recovers_only_after_same_profile_valid_review(issue):
    error = {'stage': 'expansion', 'profile': 'qwen', 'error_type': 'IncompleteModelOutput'}
    errors = [error]
    reviews = [review('qwen')]
    checks = {'item_arithmetic': 'pass', 'receipt_arithmetic': 'pass'}
    c = candidate()
    if issue == 'search': errors.append({'stage': 'expanded_search', 'error_type': 'OSError'})
    if issue == 'citation': reviews[0]['output']['source_ids'] = ['item:canonical']
    if issue == 'arithmetic': checks['item_arithmetic'] = 'fail'
    if issue == 'unknown_arithmetic': checks['item_arithmetic'] = 'not_checkable'
    if issue == 'weak': c['confidence'] = .6
    if issue == 'title': reviews[0]['output']['candidate_title'] = 'Different Brand'
    if issue == 'profile': error['profile'] = 'qwen-other'
    blocking, recovered = collab.classify_discovery_errors(errors, [c], reviews, checks, .85,
        issue != 'context', issue != 'unverified')
    if issue is None:
        assert blocking == [] and recovered == [error]
    else:
        assert blocking == errors and recovered == []
    assert error in errors


@pytest.mark.parametrize('repaired', [True, False])
def test_review_product_title_must_match_id_and_gets_one_retry(monkeypatch, repaired):
    calls = []
    def request(profile, text, schema, images):
        calls.append(text)
        assert schema['properties']['candidate_title']['enum'] == [None, TITLE]
        output = review('qwen', refs=[SOURCE])['output']
        if len(calls) == 1 or not repaired: output['candidate_title'] = 'Que Pasa Picante Medium'
        return {'output': output}
    monkeypatch.setattr(providers, 'request', request)
    result = collab.review_with_citations(providers.Profile('qwen', 'qwen', 'test'), 'evidence',
        {SOURCE}, [candidate()], [], {})
    assert len(calls) == 2 and 'title did not match' in calls[1]
    assert result['status'] == ('success' if repaired else 'error')
    assert result['attempts'][0]['invalid_output']['candidate_title'] == 'Que Pasa Picante Medium'
    if repaired: assert result['output']['candidate_title'] == TITLE


def test_recovered_expansion_is_audited_without_redundant_openai_review(monkeypatch):
    calls = []
    def search(key, query, item, domain):
        return [core.SearchResult(TITLE, URL, '', .9167)] if TITLE in query else []
    def request(profile, text, schema, images):
        stage = 'review' if 'candidate_id' in schema['properties'] else 'expansion'
        calls.append((profile.provider, stage))
        if stage == 'expansion' and profile.provider == 'qwen':
            raise providers.IncompleteModelOutput({'finish_reason': 'length', 'output_tokens': profile.output_tokens})
        if stage == 'expansion':
            target = json.loads(text.splitlines()[-1])['target_reading']
            output = {'reading': 'Different Brand hypothesis', 'queries': [TITLE],
                      'source_ids': target['source_ids'][:1], 'reason': 'unverified'}
        else:
            assert 'Different Brand hypothesis' not in text
            assert 'use the retailer title' in text
            output = review('qwen', refs=[SOURCE])['output']
        return {'output': output}
    monkeypatch.setattr(core, 'brave_candidates', search)
    monkeypatch.setattr(providers, 'request', request)
    data = bundle()
    data['validations']['item_arithmetic'] = 'pass'
    payload = collab.collaborate_item({'item_name': 'Cep Pic Med', 'store_name': 'Sobeys'}, data,
        [providers.Profile('openai', 'openai', 'test'), providers.Profile('qwen', 'qwen', 'test')],
        'key', .85, {'receipt': 'a.pdf'})
    assert calls == [('qwen', 'expansion'), ('qwen', 'expansion'), ('openai', 'expansion'), ('qwen', 'review')]
    assert payload['decision']['disposition'] == 'recommended'
    assert payload['decision']['candidate_title'] == TITLE
    assert payload['decision']['review_policy']['skipped_profiles'] == ['openai']
    assert len(payload['errors']) == 1 and payload['recovered_errors'] == payload['errors']
    assert payload['blocking_errors'] == []


def test_truncated_qwen_sample_survives_logging_and_attempt_history(monkeypatch, capsys):
    monkeypatch.setattr(providers, 'post', lambda *args: {
        'done': True, 'done_reason': 'length', 'message': {'content': '{"reading":"' + 'repeat ' * 1000},
        'eval_count': 2048, 'prompt_eval_count': 623})
    result = collab.expand_readings([{'reading': 'Oep Pic Med', 'source_ids': [SOURCE]}],
        [providers.Profile('qwen', 'qwen', 'test')], 'Sobeys', {})[0]
    assert result['status'] == 'error' and 'output' not in result
    assert len(result['attempts']) == 2
    for attempt in result['attempts']:
        sample = attempt['incomplete_output']
        assert sample['content_chars'] > 1024
        assert len(sample['content_head']) + len(sample['content_tail']) == 1024
    logs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert any(log.get('incomplete_output') == result['incomplete_output'] for log in logs)


def test_qwen_retry_grows_reasoning_budget_and_stops_at_ceiling():
    profile = providers.Profile('qwen', 'qwen', 'test', 32768, 8192)
    retry = collab.qwen_retry_profile(profile)
    assert retry.output_tokens == 16384
    assert retry.context_tokens == 32768 and profile.output_tokens == 8192
    assert collab.qwen_retry_profile(retry) is None


def test_partial_qwen_queries_can_verify_without_a_correction(monkeypatch):
    calls, searched = [], []
    def request(profile, *args):
        calls.append(profile.name)
        return {'output': {'reading': 'Salsa', 'queries': ['Salsa Pic Med'],
                          'source_ids': [SOURCE], 'reason': 'Unverified hypothesis'}}
    def verify(result):
        searched.extend(result['output']['queries'])
        return True
    monkeypatch.setattr(providers, 'request', request)
    results = collab.expand_readings([{'reading': 'Oep Pic Med', 'source_ids': [SOURCE]}],
        [providers.Profile('qwen', 'qwen', 'test'), providers.Profile('openai', 'openai', 'test')],
        'Sobeys', {}, verify=verify)
    assert calls == ['qwen'] and searched == ['Salsa Pic Med']
    assert results[0]['verified_search_match'] is True


def test_qwen_partial_queries_move_to_alternate_reading_without_semantic_retry(monkeypatch):
    targets, searched = [], []
    def request(profile, text, *args):
        target = json.loads(text.splitlines()[-1])['target_reading']
        targets.append((profile.provider, target['reading']))
        return {'output': {'reading': target['reading'], 'queries': ['Cepacol Pic Med'],
                          'source_ids': [SOURCE], 'reason': 'Unverified hypothesis'}}
    monkeypatch.setattr(providers, 'request', request)
    hypotheses = [{'reading': r, 'source_ids': [SOURCE]} for r in ('Cep Pic Med', 'Oep Pic Med')]
    results = collab.expand_readings(hypotheses, [providers.Profile('qwen', 'qwen', 'test')],
        'Sobeys', {}, verify=lambda r: searched.append(r['output']['queries']) or False)
    assert targets == [('qwen', 'Cep Pic Med'), ('qwen', 'Oep Pic Med')]
    assert len(searched) == 2
    assert all(r['status'] == 'success' and r['expansion_state'] == 'unresolved' for r in results)
    assert all(r['correction_skipped'] == 'try_other_ocr_readings' for r in results)


def test_failed_semantic_correction_preserves_valid_first_expansion(monkeypatch):
    calls = []
    def request(*args):
        calls.append(1)
        if len(calls) == 2:
            raise providers.IncompleteModelOutput({'finish_reason': 'length'})
        return {'output': {'reading': 'Salsa', 'queries': ['Salsa Pic Med'],
                          'source_ids': [SOURCE], 'reason': 'Unverified hypothesis'}}
    monkeypatch.setattr(providers, 'request', request)
    result = collab.expand_readings([{'reading': 'Oep Pic Med', 'source_ids': [SOURCE]}],
        [providers.Profile('openai', 'openai', 'test')], 'Sobeys', {})[0]
    assert result['status'] == 'success' and result['correction_failed'] is True
    assert result['output']['queries'] == ['Salsa Pic Med']
    assert result['attempts'][1]['error_type'] == 'IncompleteModelOutput'


@pytest.mark.parametrize('stage', ['expansion', 'review'])
def test_thinking_only_truncation_does_not_repeat_a_larger_budget(monkeypatch, stage):
    calls = []
    def request(profile, *args):
        calls.append(profile.output_tokens)
        raise providers.IncompleteModelOutput({'finish_reason': 'length', 'output_tokens': profile.output_tokens,
            'incomplete_output': {'content_chars': 0, 'thinking_chars': 21887}})
    monkeypatch.setattr(providers, 'request', request)
    profile = providers.Profile('qwen', 'qwen', 'test', 32768, 8192)
    if stage == 'expansion':
        result = collab.expand_readings([{'reading': 'Gep Pic Med', 'source_ids': [SOURCE]}],
            [profile], 'Sobeys', {})[0]
        assert result['stop_reason'] == 'provider_truncation_circuit_open'
    else:
        result = collab.review_with_citations(profile, 'evidence', {SOURCE}, [candidate()], [], {})
    assert calls == [8192]
    assert result['retry_skipped_reason'] == 'thinking_only_truncation'
    assert result['status'] == 'error' and len(result['attempts']) == 1


@pytest.mark.parametrize('unknown_tail', [False, True])
def test_review_overflow_preserves_audit_and_support_but_rejects_unknown_tail(monkeypatch, unknown_tail):
    refs = [f'pass:other:{i}' for i in range(22)] + [SOURCE]
    sources = set(refs)
    if unknown_tail:
        refs.append('invented:tail')
    calls = []
    def request(profile, text, schema, images):
        calls.append(schema)
        return {'output': review('qwen', refs=refs)['output'], 'output_tokens': 1157}
    monkeypatch.setattr(providers, 'request', request)
    result = collab.review_with_citations(providers.Profile('qwen', 'qwen', 'test'),
        'evidence', sources, [candidate()], [], {})
    assert calls[0]['properties']['source_ids']['maxItems'] == 6
    assert set(calls[0]['properties']['source_ids']['items']['enum']) == sources
    if unknown_tail:
        assert result['status'] == 'error' and result['error_type'] == 'EvidenceCitationError'
        assert len(calls) == 2 and 'citation_normalization' not in result
    else:
        assert len(calls) == 1 and result['status'] == 'success'
        assert len(result['output']['source_ids']) == 6 and SOURCE in result['output']['source_ids']
        assert result['citation_normalization']['original_source_ids'] == refs
        assert result['citation_normalization']['original_count'] == 23
        assert result['output_tokens'] == 1157


@pytest.mark.parametrize('paid_fallback', ['0', '1'])
def test_collaboration_uses_qwen_only_unless_paid_fallback_is_explicit(monkeypatch, paid_fallback):
    profiles = [providers.Profile('openai', 'openai', 'paid'),
                providers.Profile('qwen', 'qwen', 'local'),
                providers.Profile('anthropic', 'anthropic', 'paid')]
    monkeypatch.setattr(providers, 'profiles', lambda: (profiles[:], []))
    monkeypatch.setenv('COLLAB_PAID_FALLBACK', paid_fallback)
    selected, skipped = collab.collaboration_profiles()
    assert [p.provider for p in selected] == (['qwen'] if paid_fallback == '0' else ['openai', 'qwen', 'anthropic'])
    assert {p['provider'] for p in skipped} == ({'openai', 'anthropic'} if paid_fallback == '0' else set())


def test_evidence_guided_qwen_resolves_without_any_paid_model_call(monkeypatch):
    calls, searches = [], []
    def search(key, query, item, domain):
        searches.append(query)
        if 'picante medium' in query.lower():
            return [core.SearchResult(TITLE, URL, '', .9167)]
        return [core.SearchResult('Salsa Picante range', 'https://sobeys.com/recipes/salsa', 'Medium salsa', .2)]
    def request(profile, text, schema, images):
        calls.append((profile.provider, schema))
        assert profile.provider == 'qwen'
        if schema == collab.EXPANSION_SCHEMA:
            evidence = json.loads(text.splitlines()[-1])
            assert evidence['retailer_search_evidence'][0]['title'] == 'Salsa Picante range'
            assert 'omit it from at least one query' in text
            return {'output': {'reading': 'Unverified salsa hypothesis', 'queries': ['picante medium'],
                'source_ids': ['item:canonical'], 'reason': 'Search the product/style tokens; brand uncertain'}}
        return {'output': review('qwen', refs=[SOURCE])['output']}
    monkeypatch.setattr(core, 'brave_candidates', search)
    monkeypatch.setattr(providers, 'request', request)
    monkeypatch.setattr(shared, 'render_images', lambda _: ([{'id': 'image:1:1', 'data': 'image'}], []))
    evidence = bundle()
    evidence['validations']['item_arithmetic'] = 'pass'
    payload = collab.collaborate_item({'item_name': 'Cep Pic Med', 'store_name': 'Sobeys'}, evidence,
        [providers.Profile('qwen', 'qwen', 'local', vision=True)], 'key', .85, {'receipt': 'a.pdf'})
    assert payload['decision']['disposition'] == 'recommended'
    assert payload['decision']['candidate_title'] == TITLE
    assert len(calls) == 1 and all(p == 'qwen' for p, _ in calls)
    assert payload['discovery']['verified_literal_match'] is True
    assert payload['decision']['canonical_updated'] is False
    assert any('picante medium' in q for q in searches)


def test_brand_only_retailer_hits_do_not_steer_qwen_and_cannot_be_selected(monkeypatch):
    medicine_url = 'https://sobeys.com/products/cepacol-honey-lemon-lozenges'
    medicine_title = 'Cepacol Extra Strength Honey And Lemon Lozenges'
    calls = []
    monkeypatch.setattr(core, 'brave_candidates', lambda *args: [
        core.SearchResult(medicine_title, medicine_url, '', .3)])
    def request(profile, text, schema, images):
        calls.append(text)
        assert medicine_title not in text
        if schema in (collab.EXPANSION_SCHEMA, collab.PROPOSAL_SCHEMA):
            sources = json.loads(text.splitlines()[-1]).get('target_reading', {}).get('source_ids', [SOURCE])
            return {'output': {'reading': 'Unresolved', 'queries': [] if schema == collab.EXPANSION_SCHEMA else ['size medium'],
                'source_ids': sources[:1], 'reason': 'No supporting product/style evidence'}}
        assert schema['properties']['candidate_id']['enum'] == [None]
        return {'output': {'candidate_id': None, 'candidate_title': None, 'product_source_id': None,
            'source_ids': [SOURCE], 'reason': 'No verified candidate'}}
    monkeypatch.setattr(providers, 'request', request)
    result = collab.collaborate_item({'item_name': 'Cep Pic Med', 'store_name': 'Sobeys'}, bundle(),
        [providers.Profile('qwen', 'qwen', 'test')], 'key', .85, {'receipt': 'a.pdf'})
    assert result['decision']['disposition'] == 'review'
    assert result['decision']['candidate_id'] is None
    assert result['candidates'] == []
    assert result['discovery']['relaxed_descriptive_queries'] == ['site:sobeys.com pic', 'site:sobeys.com med']
    assert result['discovery']['grounded_descriptive_queries'] == []
    assert all(medicine_title not in text for text in calls)


def test_descriptive_evidence_preserves_product_words_without_a_brand_mapping():
    medicine = core.SearchResult('Cepacol Honey Lemon Lozenges', 'https://sobeys.com/products/cepacol', '', .3)
    salsa = core.SearchResult(TITLE, URL, '', .9167)
    assert collab.descriptive_token_evidence('Cep Pic Med', 'Sobeys', medicine) is None
    evidence = collab.descriptive_token_evidence('Gep Pic Med', 'Sobeys', salsa)
    assert [t['matched'] for t in evidence['tokens'][1:]] == ['picante', 'medium']
    assert evidence['tokens'][0]['kind'] == 'unmatched'


@pytest.mark.parametrize('brand', ['ep', 'No', 'Cep', 'Oep'])
def test_short_or_filtered_brand_never_drops_descriptive_pic_token(brand):
    medicine = core.SearchResult('Medicine and Media', 'https://sobeys.com/category/medicine', '', 0)
    salsa = core.SearchResult(TITLE, URL, '', .9167)
    assert collab.descriptive_token_evidence(f'{brand} Pic Med', 'Sobeys', medicine) is None
    evidence = collab.descriptive_token_evidence(f'{brand} Pic Med', 'Sobeys', salsa)
    assert [t['token'] for t in evidence['descriptive_tokens']] == ['pic', 'med']
    assert [t['matched'] for t in evidence['descriptive_tokens']] == ['picante', 'medium']


def test_short_brand_discovery_keeps_both_tokens_in_grounded_queries(monkeypatch):
    searches, prompts = [], []
    def search(key, query, item, domain):
        searches.append(query)
        if query == 'site:sobeys.com picante medium':
            return [core.SearchResult(TITLE, URL, '', .9167)]
        return [core.SearchResult('Medicine and Media', 'https://sobeys.com/category/medicine', '', 0),
                core.SearchResult('Salsa Picante Medium Recipes', 'https://sobeys.com/recipes/salsa', '', 0)]
    def request(profile, text, schema, images):
        prompts.append(text)
        assert 'Medicine and Media' not in text
        return {'output': review('qwen', refs=[SOURCE])['output']}
    evidence = bundle()
    evidence['sources'].append({'id': 'pass:short:1', 'kind': 'ocr_pass', 'engine': 'tesseract', 'text': 'ep Pic Med'})
    evidence['validations']['item_arithmetic'] = 'pass'
    monkeypatch.setattr(core, 'brave_candidates', search)
    monkeypatch.setattr(providers, 'request', request)
    monkeypatch.setattr(shared, 'render_images', lambda _: ([{'id': 'image:1:1', 'data': 'image'}], []))
    payload = collab.collaborate_item({'item_name': 'Cep Pic Med', 'store_name': 'Sobeys'}, evidence,
        [providers.Profile('qwen', 'qwen', 'local', vision=True)], 'key', .85, {'receipt': 'a.pdf'})
    assert payload['discovery']['grounded_descriptive_queries'] == ['site:sobeys.com picante medium']
    assert 'site:sobeys.com medicine' not in searches and 'site:sobeys.com media' not in searches
    assert payload['decision']['disposition'] == 'recommended'
    assert len(prompts) == 1 and payload['decision']['canonical_updated'] is False


def test_relaxed_descriptor_retrieval_grounds_full_query_before_qwen(monkeypatch):
    searches, calls = [], []
    def search(key, query, item, domain):
        searches.append(query)
        assert item == 'Cep Pic Med' and domain == 'sobeys.com'
        if query == 'site:sobeys.com pic':
            return [core.SearchResult('Picante Medium Salsa Recipes', 'https://sobeys.com/recipes/salsa', '', 0)]
        if query == 'site:sobeys.com picante medium':
            return [core.SearchResult(TITLE, URL, '', .9167)]
        return [core.SearchResult('Medicine', 'https://sobeys.com/category/medicine', '', 0)]
    def request(profile, text, schema, images):
        calls.append(profile.provider)
        assert profile.provider == 'qwen' and 'Medicine' not in text
        assert TITLE in text and images
        return {'output': review('qwen', refs=[SOURCE])['output']}
    monkeypatch.setattr(core, 'brave_candidates', search)
    monkeypatch.setattr(providers, 'request', request)
    monkeypatch.setattr(shared, 'render_images', lambda _: ([{'id': 'image:1:1', 'data': 'image'}], []))
    evidence = bundle()
    evidence['validations']['item_arithmetic'] = 'pass'
    result = collab.collaborate_item({'item_name': 'Cep Pic Med', 'store_name': 'Sobeys'}, evidence,
        [providers.Profile('qwen', 'qwen', 'local', vision=True)], 'key', .85, {'receipt': 'a.pdf'})
    assert result['discovery']['relaxed_descriptive_queries'] == ['site:sobeys.com pic', 'site:sobeys.com med']
    assert result['discovery']['grounded_descriptive_queries'] == ['site:sobeys.com picante medium']
    assert result['discovery']['expansion_calls'] == 0
    assert result['decision']['candidate_title'] == TITLE
    assert result['decision']['disposition'] == 'recommended'
    assert result['decision']['canonical_updated'] is False and calls == ['qwen']
    assert len(searches) == len(set(searches))
