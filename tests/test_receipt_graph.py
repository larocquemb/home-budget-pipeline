from copy import deepcopy
import json

import pytest

from home_budget_pipeline import receipt_graph as g
from home_budget_pipeline.web import receipt_graph as web
from fastapi import HTTPException


def evidence():
    source = {'id': 'pass:ocr-1:1:line:1', 'kind': 'ocr_pass', 'text': 'Oep Pic Med',
              'ocr_run_uuid': 'ocr-1', 'pass_id': 1, 'engine': 'paddle'}
    p = {'run_uuid': 'compare-1', 'item_id': 10, 'worker_identity': {'worker_host': 'job-1', 'worker_node': 'arsene'},
         'evidence_bundle': {'sources': [source], 'validations': {'receipt_arithmetic': 'pass'}},
         'prompt_source_ids': [source['id']], 'images': [{'id': 'image:1:1', 'sha256': 'b'*64, 'page_number': 1}],
         'search_queries': [{'query': 'Old El Paso picante medium', 'candidate_ids': ['product:1']}],
         'search_results': [{'id': 'product:1', 'title': 'Old El Paso Picante Medium', 'url': 'https://sobeys.com/products/1',
                             'confidence': .9167, 'ocr_support': [source['id']], 'evidence': {'tokens': [{'weight': 1}]}}],
         'proposals': [], 'reviews': [], 'decision': {'disposition': 'recommended', 'candidate_id': 'product:1', 'canonical_updated': False}}
    for provider in ['openai', 'qwen']:
        base = {'profile': provider, 'provider': provider, 'model': provider+'-model', 'status': 'success',
                'input_tokens': 100, 'output_tokens': 50, 'seconds': 2, 'image_count': 1 if provider == 'openai' else 0}
        p['proposals'].append({**base, 'stage': 'proposal', 'output': {'reading': 'Old El Paso Picante Medium', 'queries': ['picante'], 'source_ids': [source['id']], 'reason': 'Evidence'}})
        p['reviews'].append({**base, 'stage': 'review', 'output': {'candidate_id': 'product:1', 'source_ids': [source['id'], 'product:1'], 'reason': 'Evidence'}})
    return {'receipt': {'source_sha256': 'a'*64, 'source_reference': 'receipt.pdf'}, 'expense': {'id': 1, 'store_name': 'Sobeys'},
            'items': [{'id': 10, 'item_name': 'Cep Pic Med', 'budget_category': 'Groceries'}],
            'ocr_runs': [{'run_uuid': 'ocr-1', 'worker_host': 'worker-1', 'worker_identity': {'node': 'longbow'}}],
            'ocr_passes': [{'run_uuid': 'ocr-1', 'pass_id': 1, 'engine': 'paddle', 'seconds': 4}],
            'collaborations': [{'payload': p}]}


def test_graph_connects_shared_evidence_without_accepting_proposals():
    graph = g.build(evidence())
    nodes = {n['id']: n for n in graph['nodes']}
    assert all(e['source'] in nodes and e['target'] in nodes for e in graph['edges'])
    kinds = {e['kind'] for e in graph['edges']}
    assert {'SUPPORTS', 'CITES', 'CONSIDERED_PROPOSAL', 'RECEIVED', 'RECOMMENDS', 'RUNS_ON', 'PERSISTED_AS'} <= kinds
    assert 'ACCEPTED' not in kinds
    qwen = [n for n in nodes.values() if n['kind'] == 'ModelInvocation' and n['properties']['provider'] == 'qwen']
    images = {n['id'] for n in nodes.values() if n['kind'] == 'ImageReference'}
    assert not any(e['target'] in images and e['source'] in {n['id'] for n in qwen} for e in graph['edges'])
    ocr = next(n for n in nodes.values() if n['kind'] == 'OCRPass')
    assert ocr['properties']['seconds'] == 4
    assert g.build(evidence()) == graph


def test_expansion_graph_keeps_each_reading_and_links_actual_search():
    data = evidence()
    payload = data['collaborations'][0]['payload']
    source = payload['evidence_bundle']['sources'][0]['id']
    payload['expansions'] = [{
        'profile': 'openai', 'provider': 'openai', 'model': 'test', 'stage': 'expansion',
        'target_reading': reading, 'status': 'success', 'input_source_ids': [source],
        'image_count': 0, 'searched_queries': ['Old El Paso picante medium'],
        'output': {'reading': 'Old El Paso Picante Medium', 'source_ids': [source]}}
        for reading in ['Cep Pic Med', 'Oep Pic Med']]
    graph = g.build(data)
    invocations = [n for n in graph['nodes'] if n['kind'] == 'ModelInvocation' and n['properties']['stage'] == 'expansion']
    assert len(invocations) == 2 and len({n['id'] for n in invocations}) == 2
    assert any(e['kind'] == 'PROPOSED_SEARCH' for e in graph['edges'])
    images = {n['id'] for n in graph['nodes'] if n['kind'] == 'ImageReference'}
    assert not any(e['kind'] == 'RECEIVED' and e['source'] in {n['id'] for n in invocations}
                   and e['target'] in images for e in graph['edges'])
    assert g.build(data) == graph


def test_run_observations_and_citations_remain_separate():
    data = evidence()
    copy = deepcopy(data['collaborations'][0])
    copy['payload']['run_uuid'] = 'compare-2'
    copy['payload']['evidence_bundle']['sources'][0]['text'] = 'Changed reading'
    copy['payload']['reviews'][0]['status'] = 'error'
    copy['payload']['reviews'][0]['invalid_output'] = copy['payload']['reviews'][0].pop('output')
    copy['payload']['reviews'][0]['invalid_output']['source_ids'] = ['invented']
    copy['payload']['decision'] = {'disposition': 'review', 'canonical_updated': False}
    data['collaborations'].append(copy)
    graph = g.build(data)
    observations = [n for n in graph['nodes'] if n['kind'] == 'Observation']
    assert len(observations) == 2
    assert {n['properties']['text'] for n in observations} == {'Changed reading', 'Oep Pic Med'}
    assert any(e['kind'] == 'INVALID_CITATION' for e in graph['edges'])
    assert len([n for n in graph['nodes'] if n['kind'] == 'Item']) == 1


def test_reused_database_ids_do_not_merge_different_receipts():
    first = evidence()
    second = evidence()
    second['receipt']['source_sha256'] = 'b'*64
    item_ids = [next(n['id'] for n in g.build(data)['nodes'] if n['kind'] == 'Item') for data in (first,second)]
    assert item_ids[0] != item_ids[1]


def test_learned_queries_link_prior_matches_without_becoming_current_receipt_evidence():
    data = evidence()
    payload = data['collaborations'][0]['payload']
    query = payload['search_queries'][0]['query']
    payload['learned_searches'] = [{'query': query, 'prior_matches': [{
        'cache_id': 7, 'receipt_text': 'oep pic med', 'product_title': 'Old El Paso Picante Medium',
        'product_url': 'https://sobeys.com/products/old-el-paso-medium'}]}]
    result = g.build(data)
    ids = {n['id'] for n in result['nodes'] if n['kind'] == 'PriorProductMatch'}
    assert len(ids) == 1
    assert any(e['kind'] == 'LEARNED_FROM' and e['target'] in ids for e in result['edges'])
    assert not any(e['kind'] in {'CITES', 'SUPPORTS', 'RECEIVED'} and e['target'] in ids for e in result['edges'])


def test_retry_requires_an_observed_retry_not_merely_multiple_runs():
    data = evidence()
    data['events'] = [{'id': str(i), 'occurred_at': str(i), 'payload': {
        'message_id': 'message', 'operation': 'receipt.cache-rebuild.v3', 'attempt': attempt,
        'attempt_id': ident, 'status': status}} for i, (ident, attempt, status) in enumerate([
            ('first', 1, 'active'), ('first', 1, 'retried'), ('second', 2, 'active'), ('second', 2, 'cache_ready')])]
    result = g.build(data)
    assert len([e for e in result['edges'] if e['kind'] == 'RETRIED_AS']) == 1
    data['events'] = [e for e in data['events'] if e['payload']['status'] != 'retried']
    assert not any(e['kind'] == 'RETRIED_AS' for e in g.build(data)['edges'])


def test_web_graph_is_bounded_and_handles_missing_backend(monkeypatch):
    graph = g.build(evidence())
    monkeypatch.setattr(web, 'read', lambda *a, **kw: [{'graph': json.dumps(graph), 'observed_at': 'now'}])
    result = web.receipt('a'*64, perspective='domain', offset=0, limit=3)
    assert len(result['nodes']) == 3
    ids = {n['id'] for n in result['nodes']}
    assert all(e['source'] in ids and e['target'] in ids for e in result['edges'])
    monkeypatch.delenv('NEO4J_URI', raising=False)
    # Call the original helper directly, not the patched stub.
    with pytest.raises(HTTPException) as error:
        ORIGINAL_READ('RETURN 1')
    assert error.value.status_code == 503


ORIGINAL_READ = web.read


def test_evidence_links_do_not_allow_script_urls():
    node = {'kind': 'SearchResult', 'properties': {'url': 'javascript:alert(1)'}}
    assert all(not l['url'].startswith('javascript:') for l in web.links(node))


def test_http_graph_routes_require_identity_and_keep_credentials_server_side(monkeypatch):
    from fastapi.testclient import TestClient
    from home_budget_pipeline.web.receipt_app import app
    monkeypatch.delenv('LEDGER_PROXY_SECRET', raising=False)
    monkeypatch.setenv('NEO4J_PASSWORD', 'server-only-test-password')
    monkeypatch.setattr(web, 'read', lambda *a, **kw: [])
    with TestClient(app) as client:
        assert client.get(web.BASE_PATH + '/api/graph/receipts').status_code == 401
        headers = {'X-Forwarded-User': 'test-operator'}
        assert client.get(web.BASE_PATH + '/api/graph/receipts', headers=headers).json() == []
        response = client.get(web.BASE_PATH + '/graph', headers=headers)
        assert response.status_code == 200
        assert 'server-only-test-password' not in response.text
        assert 'Explore related receipts' in response.text
        assert client.get(web.BASE_PATH + '/api/graph/receipts/invalid', headers=headers).status_code == 422


def test_explicit_product_citation_projects_without_mutating_review():
    data = evidence()
    for review in data['collaborations'][0]['payload']['reviews']:
        review['output']['product_source_id'] = 'product:1'
        review['output']['source_ids'].remove('product:1')
    before = deepcopy(data)
    graph = g.build(data)
    nodes = {n['id']: n for n in graph['nodes']}
    reviews = {n['id'] for n in graph['nodes'] if n['kind'] == 'ModelReview'}
    product_links = [e for e in graph['edges'] if e['kind'] == 'CITES' and e['source'] in reviews
                     and nodes[e['target']]['kind'] == 'SearchResult']
    assert len(product_links) == 2
    assert data == before


def test_graph_review_label_uses_verified_retailer_title_and_retains_recovery_audit():
    data = evidence()
    payload = data['collaborations'][0]['payload']
    title = payload['search_results'][0]['title']
    payload['decision']['recovered_discovery_errors'] = [{'profile': 'qwen', 'stage': 'expansion', 'error_type': 'IncompleteModelOutput'}]
    for review in payload['reviews']:
        review['output'].update(candidate_title=title, reason='Unverified different brand hypothesis')
    graph = g.build(data)
    assert all(n['label'] == title for n in graph['nodes'] if n['kind'] == 'ModelReview')
    decision = next(n for n in graph['nodes'] if n['kind'] == 'Decision')
    assert json.loads(decision['properties']['recovered_discovery_errors']) == payload['decision']['recovered_discovery_errors']


def test_descriptor_proposal_tracks_derivation_search_and_review():
    data = evidence()
    payload = data['collaborations'][0]['payload']
    source = payload['evidence_bundle']['sources'][0]['id']
    payload['descriptor_expansions'] = [{
        'profile': 'qwen', 'provider': 'qwen', 'model': 'managed-vl', 'stage': 'descriptive_expansion',
        'descriptive_tokens': ['pic', 'med'], 'derived_source_ids': [source], 'status': 'success',
        'image_count': 0, 'searched_queries': ['Old El Paso picante medium'],
        'output': {'queries': ['picante medium', 'pictorial medium', 'piccolo medium']}}]
    graph = g.build(data)
    invocation = next(n for n in graph['nodes'] if n['kind'] == 'ModelInvocation'
                      and n['properties']['stage'] == 'descriptive_expansion')
    proposal = next(n for n in graph['nodes'] if n['kind'] == 'Proposal' and n['label'] == 'picante medium')
    assert any(e['kind'] == 'PRODUCED' and e['source'] == invocation['id'] and e['target'] == proposal['id'] for e in graph['edges'])
    assert {'DERIVED_FROM', 'PROPOSED_SEARCH'} <= {e['kind'] for e in graph['edges'] if e['source'] == proposal['id']}
    assert any(e['kind'] == 'CONSIDERED_PROPOSAL' and e['target'] == proposal['id'] for e in graph['edges'])
    assert not any(e['kind'] == 'RECEIVED' and e['source'] == invocation['id'] for e in graph['edges'])
