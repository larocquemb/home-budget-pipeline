import pytest
from decimal import Decimal
from home_budget_pipeline.receipt_ocr_alternatives import align, alternatives


def pass_record(text, pass_id=1, **extra):
    return dict(extracted_text=text, evidence_id=7, run_uuid='run-a',
                pass_id=pass_id, status='success', page_number=1,
                engine='tesseract', variant='raw', **extra)


def test_repeated_purchases_stay_separate_and_keep_provenance():
    items = [dict(id=1, item_name='Cep Pic Med', line_total=Decimal('6.49')),
             dict(id=2, item_name='Cep Pic Med', line_total=Decimal('6.49'))]
    documents = [dict(id=7, raw_text='GROCERY\nCep Pic Med $6.49 C\nCep Pic Med $6.49 C')]
    passes = [pass_record('GROCERY\nOep Pic Med $6.49 C\nGep Pic Med $6.49 C'),
              pass_record('Cep Pic Med $6.49 C\nCep Pic Med $6.49 C', 2)]
    result = alternatives(items, documents, passes)
    assert [r['text'] for r in result[1]] == ['Cep Pic Med', 'Oep Pic Med']
    assert [r['text'] for r in result[2]] == ['Cep Pic Med', 'Gep Pic Med']
    source = result[1][1]['sources'][0]
    assert (source['run_uuid'], source['pass_id'], source['line_number']) == ('run-a', 1, 2)
    assert result[2][1]['sources'][0]['line_number'] == 3


def test_missing_duplicate_is_ambiguous_and_not_assigned_to_either_purchase():
    assert align(['Cep Pic Med $6.49', 'Cep Pic Med $6.49'], ['Oep Pic Med $6.49']) == {}


def test_different_prices_and_unrelated_rows_are_excluded():
    assert align(['Cep Pic Med $6.49'], ['Oep Pic Med $8.49']) == {}
    assert align(['Cep Pic Med $6.49'], ['Pie Filling $6.49']) == {}


def test_inserted_discount_does_not_shift_item_associations():
    assert align(['Cep Pic Med $6.49', 'Pretzels HoneyMustar $5.49'],
                 ['Oep Pic Med $6.49', 'YOU SAVED $1.00', 'Pretzels HoneyMustar $5.49']) == {0: 0, 1: 2}


def test_spelling_deduplication_keeps_all_sources():
    result = alternatives([dict(id=1, item_name='Cep Pic Med', line_total=None)],
                          [dict(id=7, raw_text='Cep Pic Med $6.49')],
                          [pass_record('Oep Pic Med $6.49'), pass_record('Oep Pic Med $6.49', 2)])
    assert len(result[1]) == 2
    assert [s['pass_id'] for s in result[1][1]['sources']] == [1, 2]


@pytest.mark.parametrize("conflict", [False, True])
def test_normal_enrichment_searches_alternative_before_ai_and_persists_evidence(monkeypatch, mock_brave_query_cache, conflict):
    import psycopg
    from home_budget_pipeline import product_enrichment as core
    from home_budget_pipeline import receipt_ocr_alternatives as evidence
    import json
    calls = []
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def commit(self): pass
        def execute(self, sql, params):
            calls.append((sql, params))
            return self
        def fetchall(self):
            return [dict(id=1, item_name='Gep Pic Med', store_name='Sobeys', receipt_id=30)]
        def fetchone(self): return None
    monkeypatch.setattr(psycopg, 'connect', lambda *a, **kw: Connection())
    readings = [{'text': 'Gep Pic Med', 'sources': []},
                {'text': 'Oep Pic Med', 'sources': [{'run_uuid': 'run-a', 'pass_id': 2}]}]
    monkeypatch.setattr(evidence, 'load', lambda *args: ({1: readings}, []))
    searches = []
    def search(key, query, text, domain):
        searches.append(text)
        if text != 'Oep Pic Med':
            return core.SearchResult('Gep Pic Med', 'https://sobeys.com/products/other', '', .95) if conflict else None
        title, url = 'Old El Paso Picante Medium', 'https://sobeys.com/products/salsa'
        return core.SearchResult(title, url, '', .95 if conflict else core.candidate_score(text, domain, title, url))
    monkeypatch.setattr(core, 'brave_search', search)
    monkeypatch.setattr(core, 'ai_product_queries', lambda *args: (_ for _ in ()).throw(AssertionError('AI unnecessary')))
    stats = core.run(dsn='unused', api_key='unused', limit=1, threshold=.85, write_db=True)
    assert searches == ['Gep Pic Med', 'Oep Pic Med']
    assert stats['review' if conflict else 'accepted'] == 1
    payload = next(params[-1] for sql, params in calls if 'INSERT INTO budget.product_enrichment_results' in sql)
    assert json.loads(payload)['selected_reading'] == ('Gep Pic Med' if conflict else 'Oep Pic Med')
    assert json.loads(payload)['conflicting_products'] is conflict
    assert json.loads(payload)['readings'][1]['sources'][0]['pass_id'] == 2
    updates = [params for sql, params in calls if 'UPDATE budget.expense_items' in sql]
    assert not updates if conflict else updates[0][0] == 'Oep Pic Med'


def test_future_passes_preserve_line_confidence_and_coordinates():
    from home_budget_pipeline.receipts.ingest import OCRCandidate, OCRLine, _ocr_pass_metric
    candidate = OCRCandidate('Oep Pic Med $6.49 C',
        (OCRLine('Oep Pic Med $6.49 C', 92.5, 10, 20, 100, 30),), 450, '6')
    metric = _ocr_pass_metric(candidate, page=1, variant='high_detail', seconds=1)
    record = pass_record(candidate.text)
    record['provenance'] = metric['provenance']
    result = alternatives([dict(id=1, item_name='Cep Pic Med', line_total=Decimal('6.49'))],
                          [dict(id=7, raw_text='Cep Pic Med $6.49 C')], [record])
    source = result[1][1]['sources'][0]
    assert source['confidence'] == 92.5
    assert source['geometry'] == dict(x=10,y=20,width=100,height=30)
