"""Provenance tools share the projector and read exact published evidence."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from home_budget_pipeline import cli
from home_budget_pipeline.web import receipt_graph as web


def test_ledger_projection_uses_production_projector_entrypoint():
    with patch('home_budget_pipeline.receipt_graph.main', return_value=0) as project:
        assert cli.main(['receipts', 'graph', '--sha', 'a' * 64]) == 0
        project.assert_called_once_with(['--sha', 'a' * 64])
        project.reset_mock()
        assert cli.main(['receipts', 'graph', '--all']) == 0
        project.assert_called_once_with(['--all'])
    import tomllib
    import yaml
    scripts = tomllib.loads(Path('pyproject.toml').read_text())['project']['scripts']
    assert scripts['home-budget-receipt-graph'] == 'home_budget_pipeline.receipt_graph:main'
    production = yaml.safe_load(Path('deploy/receipt-graph/projector.yaml').read_text())['spec']
    assert production['concurrencyPolicy'] == 'Forbid'
    container = production['jobTemplate']['spec']['template']['spec']['containers'][0]
    assert container['command'] == ['ledger']
    assert container['args'] == ['receipts', 'graph', '--all']


def test_development_graph_schedule_calls_shared_projector_without_busy_restart():
    spec = importlib.util.spec_from_file_location('receipt_services', 'scripts/receipt_services.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = module.service_plist(Path('/tmp/repo'), 'graph')
    assert config['ProgramArguments'] == ['/bin/sh', '/tmp/repo/scripts/run_receipt_service.sh', 'graph']
    assert config['RunAtLoad'] is True
    assert config['KeepAlive'] is False
    assert config['StartInterval'] == 600
    assert config['StandardOutPath'].endswith('/graph.log')


@pytest.mark.parametrize('service,manifest', [
    ('worker', 'deploy/rabbitmq/receipt-worker.yaml'),
    ('worker-2', 'deploy/rabbitmq/receipt-worker-longbow.yaml'),
    ('collector', 'deploy/rabbitmq/ocr-results-collector.yaml'),
    ('graph', 'deploy/receipt-graph/projector.yaml'),
])
def test_native_and_production_dispatch_same_ledger_flow(service, manifest, tmp_path, arsene_development_environment):
    """Execute the real native wrapper with a fake Ledger; never contact services."""
    import yaml
    root = tmp_path / 'repo'
    (root / 'scripts').mkdir(parents=True)
    wrapper = root / 'scripts/run_receipt_service.sh'
    wrapper.write_text(Path('scripts/run_receipt_service.sh').read_text())
    (root / '.env.dev').touch()
    (root / '.venv/bin').mkdir(parents=True)
    (root / '.venv/bin/python').symlink_to(sys.executable)
    ledger = root / '.venv/bin/ledger'
    ledger.write_text('#!' + sys.executable + '\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n')
    ledger.chmod(0o700)
    env = {**os.environ, **arsene_development_environment, 'PYTHONPATH': str(Path('src').resolve())}
    result = subprocess.run(['/bin/sh', str(wrapper), service], env=env, check=True, capture_output=True, text=True)
    spec = yaml.safe_load(Path(manifest).read_text())['spec']
    container = (spec['jobTemplate']['spec'] if 'jobTemplate' in spec else spec)['template']['spec']['containers'][0]
    assert container['command'] == ['ledger']
    assert json.loads(result.stdout) == container['args']


@pytest.mark.parametrize('command,prefix', [(['ledger'], ['receipts', 'graph']),
    (['home-budget-receipt-graph'], [])])
def test_manual_projection_preserves_deployed_entrypoint_arguments(command, prefix, tmp_path):
    """Simulate kubectl locally for both pre-rollout and new CronJob commands."""
    fake = tmp_path / 'kubectl'
    fake.write_text('#!' + sys.executable + '''
import json,os,sys
args=sys.argv[1:]
if 'patch' in args:
    job=json.load(open(args[args.index('-f')+1]))
    job['spec']['template']['spec']['containers'][0]['args']=json.loads(args[args.index('-p')+1])[0]['value']
    print(json.dumps(job))
elif '--from=cronjob/receipt-graph' in args:
    print(json.dumps({'spec':{'template':{'spec':{'containers':[{'command':json.loads(os.environ['TEST_GRAPH_COMMAND']),'args':['--all']}]}}}}))
else:
    print(open(args[args.index('-f')+1]).read())
''')
    fake.chmod(0o700)
    env = {**os.environ, 'PATH': str(tmp_path) + os.pathsep + os.environ['PATH'],
        'SHA': 'a' * 64, 'DRY_RUN': '1', 'TEST_GRAPH_COMMAND': json.dumps(command)}
    result = subprocess.run(['bash', 'scripts/receipt_graph_job.sh'], env=env, check=True, capture_output=True, text=True)
    container = json.loads(result.stdout)['spec']['template']['spec']['containers'][0]
    assert container['command'] == command
    assert container['args'] == prefix + ['--sha', 'a' * 64]


def test_pass_comparison_uses_memberships_and_recovers_retained_lines(monkeypatch):
    nodes = {
        'run-new': {'id': 'run-new', 'kind': 'OCRRun', 'properties': {'run_uuid': 'new', 'processed_at': '2026-10-02'}},
        'run-old': {'id': 'run-old', 'kind': 'OCRRun', 'properties': {'run_uuid': 'old', 'processed_at': '2026-10-01'}},
        'pass-1': {'id': 'pass-1', 'kind': 'OCRPass', 'properties': {'run_uuid': 'new', 'pass_id': 1, 'selected_base': True,
            'extracted_text': None, 'provenance': {'lines': [{'text': 'DATE unreadable'}, {'text': 'NO. unreadable'}]}}},
        'pass-2': {'id': 'pass-2', 'kind': 'OCRPass', 'properties': {'run_uuid': 'new', 'pass_id': 2,
            'extracted_text': None, 'provenance': {'lines': [{'text': 'DATE 10/02/2026'}, {'text': 'NO. ********7654'}]}}},
        'pass-old': {'id': 'pass-old', 'kind': 'OCRPass', 'properties': {'run_uuid': 'old', 'pass_id': 1}},
    }
    calls = []
    def read(query, **params):
        calls.append((query, params))
        if 'LIMIT 2001' in query:
            return [{'id': key, 'kind': n['kind'], 'story': json.dumps({'run_uuid': n['properties']['run_uuid']})} for key, n in nodes.items()]
        if 's.version AS version' in query:
            return [{'time': 'snapshot-1', 'version': web.graph.VERSION}]
        return [{'time': 'snapshot-1'}]
    monkeypatch.setattr(web, 'read', read)
    def hydrate(sha, time, ids):
        assert sha == 'a' * 64 and time == 'snapshot-1'
        return [nodes[key] for key in ids]
    monkeypatch.setattr(web, 'snapshot_nodes', hydrate)
    data = web.ocr_readings('a' * 64)
    assert data['run_id'] == 'new'
    assert [p['id'] for p in data['passes']] == ['pass-1', 'pass-2']
    assert data['passes'][1]['properties']['reading_text'] == 'DATE 10/02/2026\nNO. ********7654'
    assert data['passes'][1]['properties']['reading_source'] == 'provenance.lines'
    assert all('ReceiptFlowEntity {id:' not in query for query, _ in calls)
    assert web.ocr_readings('a' * 64, 'old')['passes'][0]['properties']['reading_source'] == 'unavailable'
    with pytest.raises(HTTPException) as error:
        web.ocr_readings('a' * 64, 'not-this-receipts-run')
    assert error.value.status_code == 404


def test_missing_projection_is_reported_and_comparison_requires_authentication(monkeypatch):
    from home_budget_pipeline.web.receipt_app import app
    monkeypatch.delenv('LEDGER_PROXY_SECRET', raising=False)
    monkeypatch.setattr(web, 'read', lambda *a, **kw: [])
    with TestClient(app) as client:
        route = web.BASE_PATH + '/api/graph/receipts/' + 'a' * 64 + '/ocr'
        assert client.get(route).status_code == 401
        response = client.get(route, headers={'X-Forwarded-User': 'test-operator'})
        assert response.status_code == 404
        assert 'No projected snapshot' in response.json()['detail']


def test_ocr_catalog_limit_is_explicit(monkeypatch):
    def read(query, **params):
        if 's.version AS version' in query:
            return [{'time': 'snapshot', 'version': web.graph.VERSION}]
        return [{}] * 2001
    monkeypatch.setattr(web, 'read', read)
    with pytest.raises(HTTPException) as error:
        web.ocr_readings('a' * 64)
    assert error.value.status_code == 413
