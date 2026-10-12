import json
from pathlib import Path

import pytest

from home_budget_pipeline import model_evaluation as evaluation


def receipt(**changes):
    return {'merchant': 'Store', 'transaction_date': '2026-01-01', 'payment_method': None,
            'subtotal': 10.0, 'tax': 1.3, 'total': 11.3,
            'items': [{'item_name': 'Milk', 'line_total': 10.0, 'category': 'Groceries'}], **changes}


def test_partial_reference_does_not_call_extra_rows_hallucinations():
    result = evaluation.score(receipt(), {'total': 11.3})
    assert result['summary_scored'] == result['summary_matches'] == 1
    assert result['known_items_scored'] == 0
    assert result['unexpected_items'] is None
    assert result['categories_scored'] == 0


def test_duplicate_purchases_cannot_reuse_one_actual_row():
    expected = {'items': [{'item_name': 'Milk', 'line_total': 10}] * 2}
    result = evaluation.score(receipt(), expected)
    assert result['known_items_matched'] == 1
    assert len(result['missing_items']) == 1


def test_aliases_negative_values_categories_and_extra_rows():
    output = receipt(items=[{'item_name': ' MILK ', 'line_total': -10, 'category': 'Groceries'},
                            {'item_name': 'Bag', 'line_total': 0.1, 'category': None}])
    expected = {'items': [{'aliases': ['milk', 'Milk 2%'], 'line_total': -10, 'category': 'Groceries'}]}
    result = evaluation.score(output, expected, complete_items=True)
    assert result['known_items_matched'] == 1
    assert result['categories_scored'] == 1
    assert result['unexpected_items'] == [output['items'][1]]
    expected['items'][0]['category'] = 'Household'
    result = evaluation.score(output, expected)
    assert result['known_items_matched'] == 1
    assert result['categories_matched'] == 0


def test_currency_comparison_and_unknown_arithmetic():
    result = evaluation.score(receipt(total=11.30000000001), {'total': 11.3})
    assert result['summary_matches'] == 1
    assert result['subtotal_plus_tax_matches_total'] is True
    assert evaluation.score(receipt(tax=None), {})['subtotal_plus_tax_matches_total'] is None
    assert evaluation.score(receipt(total=20), {})['subtotal_plus_tax_matches_total'] is False


@pytest.mark.parametrize('output', [None, [], {}, receipt(total=True), receipt(tax=float('nan')),
    receipt(items=[{'item_name': 'Milk', 'line_total': '10', 'category': None}]),
    receipt(items=[{'item_name': '', 'line_total': 10, 'category': None}]),
    receipt(unrequested='field')])
def test_invalid_contracts_are_rejected(output):
    assert evaluation.valid_contract(output) is False


def test_valid_contract_accepts_unknown_values():
    assert evaluation.valid_contract(receipt())
    assert evaluation.valid_contract(receipt(subtotal=None, tax=None, total=None, items=[]))


def setup_run(monkeypatch, tmp_path, responses):
    manifest = tmp_path / 'cases.json'
    manifest.write_text('{}')
    cases = [{'id': 'case', 'split': 'development', 'source_sha256': 'source',
              'reference_sha256': 'reference', 'expected': {'total': 12},
              'complete_items': False,
              'images': [{'id': 'image:1:1', 'data': 'PRIVATE_IMAGE', 'sha256': 'image'}]}]
    monkeypatch.setattr(evaluation, 'load_cases', lambda *args: cases)
    monkeypatch.setattr(evaluation, 'model_snapshot', lambda base, model:
                        {'name': model, 'digest': 'digest-' + model})
    monkeypatch.setattr(evaluation, 'get_json', lambda base, path:
                        {'version': 'test'} if path == '/api/version' else {'models': []})
    monkeypatch.setenv('OLLAMA_BASE_URL', 'http://example.invalid:11434')
    monkeypatch.setattr(evaluation.GpuSampler, 'poll', lambda self: None)
    calls = []
    responses = iter(responses)
    def request(profile, text, schema, images):
        calls.append((profile, text, schema, images))
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return {'output': response, 'input_tokens': 10, 'output_tokens': 20}
    monkeypatch.setattr(evaluation.providers, 'request', request)
    args = ['--baseline', 'baseline:tag', '--candidate', 'candidate:tag', '--repeats', '1',
            '--cases', str(manifest), '--output', str(tmp_path / 'reports')]
    return args, calls


def test_quality_warnings_do_not_fail_and_inputs_are_identical(monkeypatch, tmp_path):
    args, calls = setup_run(monkeypatch, tmp_path, [receipt(), receipt(total=10)])
    assert evaluation.main(args) == 0
    assert len(calls) == 2 and calls[0][1:] == calls[1][1:]
    assert '12' not in calls[0][1]  # Expected answer is never supplied to the model.
    report_path = next((tmp_path / 'reports').glob('*/report.json'))
    raw = report_path.read_text()
    assert 'PRIVATE_IMAGE' not in raw
    report = json.loads(raw)
    assert report['models'][0]['digest'] == 'digest-baseline:tag'
    assert report['runs'][0]['quality']['summary_matches'] == 0
    assert report['cases'][0]['images'][0]['sha256'] == 'image'
    assert report['finished_at']
    assert report_path.stat().st_mode & 0o777 == 0o600
    assert report_path.parent.stat().st_mode & 0o777 == 0o700


def test_failed_request_is_saved_and_remaining_model_still_runs(monkeypatch, tmp_path):
    args, calls = setup_run(monkeypatch, tmp_path, [TimeoutError('unavailable'), receipt()])
    assert evaluation.main(args) == 1
    assert len(calls) == 2
    report = json.loads(next((tmp_path / 'reports').glob('*/report.json')).read_text())
    assert report['runs'][0]['status'] == 'error'
    assert report['runs'][1]['status'] == 'ok'


def test_invalid_json_contract_is_runtime_failure(monkeypatch, tmp_path):
    args, calls = setup_run(monkeypatch, tmp_path, [{}, receipt()])
    assert evaluation.main(args) == 1
    assert len(calls) == 2


def test_missing_input_fails_before_model_calls(monkeypatch, tmp_path):
    args, calls = setup_run(monkeypatch, tmp_path, [])
    def fail(*args):
        raise ValueError('Missing input')
    monkeypatch.setattr(evaluation, 'load_cases', fail)
    assert evaluation.main(args) == 2
    assert not calls
    assert not (tmp_path / 'reports').exists()


def test_model_change_marks_report_incomplete(monkeypatch, tmp_path):
    args, _ = setup_run(monkeypatch, tmp_path, [receipt(), receipt()])
    snapshots = iter([{'name': 'baseline:tag', 'digest': 'before'},
                      {'name': 'candidate:tag', 'digest': 'before'},
                      {'name': 'baseline:tag', 'digest': 'after'},
                      {'name': 'candidate:tag', 'digest': 'before'}])
    monkeypatch.setattr(evaluation, 'model_snapshot', lambda *args: next(snapshots))
    assert evaluation.main(args) == 1


def test_model_preflight_checks_installation_and_vision(monkeypatch):
    monkeypatch.setattr(evaluation, 'get_json', lambda *args:
                        {'models': [{'name': 'text:tag', 'digest': 'digest'}]})
    monkeypatch.setattr(evaluation.providers, 'post', lambda *args: {'capabilities': ['completion']})
    with pytest.raises(ValueError, match='not installed'):
        evaluation.model_snapshot('http://localhost', 'missing:tag')
    with pytest.raises(ValueError, match='vision'):
        evaluation.model_snapshot('http://localhost', 'text:tag')


def test_sobeys_references_are_partial_and_images_use_shared_renderer(monkeypatch, tmp_path):
    source = tmp_path / 'receipt.pdf'
    source.write_bytes(b'private PDF')
    fixture = Path(__file__).parent / 'fixtures' / 'sobeys_receipt_41_ocr.json'
    manifest = tmp_path / 'cases.json'
    manifest.write_text(json.dumps({'cases': [{'id': 'sobeys', 'source': source.name,
        'expected_fixture': str(fixture), 'complete_items': False}]}))
    calls = []
    def render(bundle, *, root):
        calls.append(bundle)
        return [{'id': 'image:1:1', 'data': 'encoded', 'sha256': 'hash'}], []
    monkeypatch.setattr(evaluation, 'render_images', render)
    cases = evaluation.load_cases(manifest, tmp_path)
    assert cases[0]['expected']['total'] == 363.95
    assert len(cases[0]['expected']['items']) > 20
    assert 'tax' not in cases[0]['expected']
    assert cases[0]['complete_items'] is False
    assert calls[0]['documents'][0]['source_sha256'] == evaluation.digest(source.read_bytes())


def test_source_escape_is_rejected(tmp_path):
    root = tmp_path / 'inputs'
    root.mkdir()
    outside = tmp_path / 'outside.pdf'
    outside.write_bytes(b'private')
    manifest = tmp_path / 'cases.json'
    manifest.write_text(json.dumps({'cases': [{'id': 'bad', 'source': '../outside.pdf'}]}))
    with pytest.raises(ValueError, match='Missing receipt'):
        evaluation.load_cases(manifest, root)


def test_overlapping_aliases_use_maximum_one_to_one_matching():
    output = receipt(items=[{'item_name': name, 'line_total': 5, 'category': None}
                            for name in ('A', 'B')])
    reference = {'items': [{'aliases': ['A', 'B'], 'line_total': 5},
                           {'item_name': 'A', 'line_total': 5}]}
    result = evaluation.score(output, reference, complete_items=True)
    assert result['known_items_matched'] == 2
    assert result['unexpected_items'] == []


@pytest.mark.parametrize('expected', [
    {'total': 'invalid'}, {'total': True}, {'tax': float('nan')},
    {'items': [{'item_name': 'Milk'}]}, {'items': [{'aliases': [], 'line_total': 5}]},
    {'unsupported': 5},
])
def test_malformed_answer_keys_cannot_be_scored(expected):
    with pytest.raises(ValueError):
        evaluation.validate_reference(expected)


def test_financial_checks_find_reward_money_and_missing_duplicate_fee():
    output = receipt(subtotal=10.24, tax=1, total=11.24, items=[
        {'item_name': 'Milk', 'line_total': 10, 'category': None},
        {'item_name': '+EHC', 'line_total': .12, 'category': None},
        {'item_name': 'POINTS EARNED', 'line_total': 200, 'category': None}])
    rules = {'reconcile_items_to_subtotal': True,
             'required_fees': [{'item_name': '+EHC', 'line_total': .12}] * 2,
             'non_monetary_patterns': ['^POINTS EARNED']}
    result = evaluation.score(output, {'subtotal': 10.24, 'tax': 1}, financial_rules=rules)
    financial = result['financial']
    assert financial['signed_item_sum'] == 210.12
    assert financial['item_sum_matches_reference_subtotal'] is False
    assert financial['fees_matched'] == 1 and financial['fees_scored'] == 2
    assert financial['non_monetary_rows'] == [output['items'][-1]]


def test_reconciliation_uses_signed_values_and_verified_subtotal():
    output = receipt(subtotal=-10, tax=-1.3, total=-11.3, items=[
        {'item_name': 'Return', 'line_total': -12, 'category': None},
        {'item_name': 'Adjustment', 'line_total': 2, 'category': None}])
    rules = {'reconcile_items_to_subtotal': True}
    financial = evaluation.score(output, {'subtotal': -10}, financial_rules=rules)['financial']
    assert financial['item_sum_matches_reference_subtotal'] is True
    output['subtotal'] = -9
    financial = evaluation.score(output, {'subtotal': -10}, financial_rules=rules)['financial']
    assert financial['item_sum_matches_reference_subtotal'] is True
    assert financial['item_sum_matches_reported_subtotal'] is False


def saved_report(tmp_path):
    reference = tmp_path / 'reference.json'
    reference.write_text(json.dumps({'source_sha256': 'source-hash',
        'expected': {'total': 11.3, 'subtotal': 10},
        'financial_rules': {'reconcile_items_to_subtotal': True}}))
    manifest = tmp_path / 'cases.json'
    manifest.write_text(json.dumps({'cases': [{'id': 'case', 'pages': [1], 'expected_file': reference.name}]}))
    source = tmp_path / 'original.json'
    source.write_text(json.dumps({'run_id': 'old-run', 'finished_at': 'original-time',
        'models': [{'name': 'model'}], 'model_digests_unchanged': True,
        'cases': [{'id': 'case', 'source_sha256': 'source-hash',
                   'images': [{'page_number': 1, 'sha256': 'image-hash'}]}],
        'runs': [{'model': 'model', 'case': 'case', 'repeat': 1, 'status': 'ok',
                  'output': receipt(), 'wall_seconds': 12.13, 'quality': {}}]}))
    return source, manifest, reference


def test_offline_rescore_preserves_outputs_timings_and_original_without_network(monkeypatch, tmp_path):
    source, manifest, _ = saved_report(tmp_path)
    original = source.read_bytes()
    def forbidden(*args, **kwargs):
        raise AssertionError('Offline rescoring must not call network or render images')
    for name in ('get_json', 'model_snapshot', 'render_images'):
        monkeypatch.setattr(evaluation, name, forbidden)
    monkeypatch.setattr(evaluation.providers, 'request', forbidden)
    assert evaluation.main(['--rescore', str(source), '--cases', str(manifest),
                            '--output', str(tmp_path / 'output')]) == 0
    result = json.loads(next((tmp_path / 'output').glob('*/report.json')).read_text())
    assert source.read_bytes() == original
    assert result['runs'][0]['output'] == receipt()
    assert result['runs'][0]['wall_seconds'] == 12.13
    assert result['rescore_of']['report_sha256'] == evaluation.digest(original)
    assert result['runs'][0]['quality']['financial']['item_sum_matches_reference_subtotal'] is True


@pytest.mark.parametrize('mismatch', ['source', 'pages'])
def test_rescore_rejects_different_receipt_evidence(tmp_path, mismatch):
    source, manifest, reference = saved_report(tmp_path)
    if mismatch == 'source':
        data = json.loads(reference.read_text()); data['source_sha256'] = 'different-source'
        reference.write_text(json.dumps(data))
    else:
        data = json.loads(manifest.read_text()); data['cases'][0]['pages'] = [2]
        manifest.write_text(json.dumps(data))
    assert evaluation.rescore_report(source, manifest, tmp_path / 'output') == 2
    assert not (tmp_path / 'output').exists()


def test_sobeys_image_reference_omits_invisible_summary_and_preserves_price_counts():
    manifest = Path('evals/receipts/cases.json')
    entry = json.loads(manifest.read_text())['cases'][0]
    expected, _, metadata = evaluation.load_reference(entry, manifest)
    assert 'merchant' not in expected and 'transaction_date' not in expected
    assert expected['tax'] == 8.09
    assert len(expected['items']) == 23
    assert expected['items'][:2] == [{'item_name': 'Cep Pic Med', 'line_total': 6.49}] * 2
    assert len(metadata['financial_rules']['required_fees']) == 2
    assert round(sum(metadata['tax_components'].values()), 2) == expected['tax']


def test_partial_rescore_stays_incomplete(tmp_path):
    source, manifest, _ = saved_report(tmp_path)
    report = json.loads(source.read_text()); report.pop('finished_at')
    source.write_text(json.dumps(report))
    assert evaluation.rescore_report(source, manifest, tmp_path / 'output') == 1
    markdown = next((tmp_path / 'output').glob('*/report.md')).read_text()
    assert 'In progress / partial' in markdown


def test_tax_component_provenance_cannot_disagree_with_combined_label(tmp_path):
    _, manifest, reference = saved_report(tmp_path)
    data = json.loads(reference.read_text())
    data['expected']['tax'] = 8.09
    data['tax_components'] = {'GST': 3.37, 'PST': 4.35}
    reference.write_text(json.dumps(data))
    entry = json.loads(manifest.read_text())['cases'][0]
    with pytest.raises(ValueError, match='sum'):
        evaluation.load_reference(entry, manifest)
