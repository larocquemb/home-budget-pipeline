import json
from pathlib import Path
import pytest
from home_budget_pipeline import enrichment_evaluation as evaluation
from home_budget_pipeline import product_comparison as comparison
from home_budget_pipeline import product_enrichment as core

CASE = {'id': 'salsa', 'receipt_item': 'Cep Pic Med', 'merchant': 'Sobeys',
        'expected_product_url': 'https://sobeys.com/products/salsa', 'expected_category': 'Groceries'}


def test_models_share_baseline_and_do_not_borrow_each_others_proposals(monkeypatch):
    candidate = core.SearchResult('Old El Paso Salsa Medium', CASE['expected_product_url'], 'salsa', .95)
    calls = []
    def generate(provider, text, **kw):
        calls.append((kw['model'], text))
        return {'queries': [kw['model']], 'model': kw['model']}
    monkeypatch.setattr(comparison, 'generate', generate)
    monkeypatch.setattr(comparison, 'baseline_search', lambda *args: [('baseline', candidate)])
    searches = []
    monkeypatch.setattr(core, 'brave_candidates', lambda key, query, item, domain:
                        searches.append((query, item)) or [])
    rows, evidence = evaluation.run([CASE], ['base', 'new'], 'secret')
    assert calls[0][1] == calls[1][1]
    assert len(searches) == 2 and all(item == CASE['receipt_item'] for _, item in searches)
    assert all(row['candidate_origin'] == 'shared_brave' for row in rows)
    assert all(row['qualification']['reference_product_match'] for row in rows)
    assert all(row['qualification']['category'] == 'Groceries' for row in rows)
    assert evidence[0]['prompt'] == calls[0][1]
    assert 'secret' not in json.dumps(evidence)


def test_incomplete_api_request_is_not_a_model_loss(monkeypatch):
    monkeypatch.setattr(comparison, 'baseline_search', lambda *args: [])
    def generate(provider, text, **kw):
        raise TimeoutError('SECRET')
    monkeypatch.setattr(comparison, 'generate', generate)
    rows, _ = evaluation.run([CASE], ['base', 'new'], 'secret')
    assert all(row['status'] == 'error' for row in rows)
    assert all(row['qualification']['reference_product_match'] is None for row in rows)
    assert 'SECRET' not in json.dumps(rows)


def test_quality_difference_and_no_match_are_complete(monkeypatch):
    monkeypatch.setattr(comparison, 'baseline_search', lambda *args: [])
    monkeypatch.setattr(comparison, 'generate', lambda provider, text, **kw: {'queries': ['salsa']})
    monkeypatch.setattr(core, 'brave_candidates', lambda *args: [])
    rows, _ = evaluation.run([CASE], ['base', 'new'], 'secret')
    assert all(row['status'] == 'no_match' for row in rows)
    assert all(row['qualification']['reference_product_match'] is False for row in rows)


def test_missing_credentials_make_informational_report_with_success_exit(monkeypatch, tmp_path):
    monkeypatch.delenv('BRAVE_API_KEY', raising=False)
    manifest = tmp_path / 'cases.json'; manifest.write_text(json.dumps({'cases': [CASE]}))
    assert evaluation.main(['--cases', str(manifest), '--output', str(tmp_path / 'reports')]) == 0
    report_path = next((tmp_path / 'reports').glob('*/report.json'))
    report = json.loads(report_path.read_text())
    assert report['incomplete'] is True
    assert all(row['qualification']['reference_category_match'] is None for row in report['results'])
    assert report_path.stat().st_mode & 0o777 == 0o600
    assert 'incomplete' in report_path.with_suffix('.md').read_text()


def test_local_settings_are_literal_filtered_and_do_not_override_environment(monkeypatch, tmp_path):
    import importlib.util
    import os
    import sys
    monkeypatch.syspath_prepend(str(Path('scripts').resolve()))
    spec = importlib.util.spec_from_file_location('eval_launcher', Path('scripts/eval_enrichment.py'))
    launcher = importlib.util.module_from_spec(spec); spec.loader.exec_module(launcher)
    path = tmp_path / 'env'
    path.write_text("BRAVE_API_KEY='literal$(do-not-run)'\nOLLAMA_BASE_URL=http://file.example\nUNRELATED_PASSWORD=unused\n")
    monkeypatch.delenv('BRAVE_API_KEY', raising=False)
    monkeypatch.delenv('UNRELATED_PASSWORD', raising=False)
    monkeypatch.setenv('OLLAMA_BASE_URL', 'http://exported.example')
    # Ensure monkeypatch restores the key assigned by the launcher afterwards.
    monkeypatch.setenv('BRAVE_API_KEY', '')
    launcher.configure_environment(path)
    assert os.getenv('BRAVE_API_KEY') == 'literal$(do-not-run)'
    assert os.getenv('OLLAMA_BASE_URL') == 'http://exported.example'
    assert os.getenv('UNRELATED_PASSWORD') is None
