import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType

import pytest


@pytest.fixture
def consolidation(monkeypatch):
    # The module runs under Ansible on the target; its planning helpers require
    # no Ansible runtime and can be tested without that optional dependency.
    for name in ('ansible', 'ansible.module_utils', 'ansible.module_utils.basic'):
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
    sys.modules['ansible.module_utils.basic'].AnsibleModule = object
    path = Path(__file__).resolve().parents[1] / 'ops/monitoring/roles/monitoring/library/grafana_consolidate.py'
    spec = importlib.util.spec_from_file_location('grafana_consolidate', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_query_comparison_preserves_nested_panels_and_repeated_queries(consolidation):
    original = {'panels': [{'type': 'row', 'panels': [
        {'title': 'CPU', 'targets': [{'expr': 'cpu'}]},
        {'title': 'CPU', 'targets': [{'expr': 'cpu'}]},
    ]}]}
    signature = consolidation.query_signature(original)
    assert sum(signature.values()) == 2
    reduced = {'panels': [{'title': 'CPU', 'targets': [{'expr': 'cpu'}]}]}
    assert signature != consolidation.query_signature(reduced)
    unique = {'panels': [{'title': 'CPU', 'targets': [{'expr': 'different_cpu'}]}]}
    assert signature != consolidation.query_signature(unique)


def test_comparison_includes_unique_text_content(consolidation):
    left = {'panels': [{'title': 'Notes', 'type': 'text', 'options': {'content': 'Keep this'}}]}
    right = {'panels': [{'title': 'Notes', 'type': 'text', 'options': {'content': 'Other'}}]}
    assert consolidation.query_signature(left) != consolidation.query_signature(right)


def test_rebind_changes_only_exact_identifiers(consolidation):
    source = {'datasource': {'uid': 'old'}, 'panels': [{'datasource': 'old'}], 'expr': 'old_metric'}
    updated = consolidation.rebind(source, 'old', 'new')
    assert updated['datasource']['uid'] == 'new'
    assert updated['panels'][0]['datasource'] == 'new'
    assert updated['expr'] == 'old_metric'
    assert source['datasource']['uid'] == 'old'


def test_infrastructure_provider_preserves_folder_identity(consolidation, tmp_path, monkeypatch):
    directory = tmp_path / 'providers'
    directory.mkdir()
    source = tmp_path / 'dashboards' / 'k3s-node.json'
    source.parent.mkdir()
    config = directory / 'nodes.yaml'
    config.write_text('providers:\n  - name: nodes\n    folder: Brown Rook\n    options:\n      path: ' + str(source.parent) + '\n')
    real_path = Path
    monkeypatch.setattr(consolidation, 'Path', lambda value: directory if value == '/etc/grafana/provisioning/dashboards' else real_path(value))
    path, original, updated = consolidation.provider_folder_update(source, 'brownrook')
    assert path == config
    assert 'folder: Infrastructure\n    folderUid: brownrook' in updated
    assert 'path: ' + str(source.parent) in updated
    config.write_text(updated)
    _, repeated, unchanged = consolidation.provider_folder_update(source, 'brownrook')
    assert repeated == unchanged


def test_private_dashboards_distinguish_execution_hosts():
    directory = Path(__file__).resolve().parents[1] / 'ops/monitoring/roles/monitoring/templates'
    for filename in ('grafana-receipt-telemetry-dashboard.json.j2',
                     'grafana-ocr-performance-dashboard.json.j2',
                     'grafana-product-comparison-dashboard.json.j2'):
        dashboard = json.loads((directory / filename).read_text())['spec']
        host = next(v['spec'] for v in dashboard['variables'] if v['spec']['name'] == 'worker_host')
        assert host['current']['value'] == ['m4pro']
        assert host['label'] == 'Execution host'
        assert host['includeAll'] is True
        links = {link['title']: link['url'] for link in dashboard['links']}
        assert 'var-worker_host=m4pro' in links['m4pro — local']
        assert 'var-worker_host=arsene' in links['Arsene — K3s']
        assert 'var-environment=%24__all' in links['Arsene — K3s']
        if 'model' not in filename and 'comparison' not in filename:
            expressions = [q['spec']['query']['spec'].get('expr', '')
                           for p in dashboard['elements'].values()
                           if p['spec']['vizConfig']['group'] == 'timeseries'
                           for q in p['spec']['data']['spec']['queries']]
            assert any('sum by (worker_host,' in query for query in expressions)
        else:
            for p in dashboard['elements'].values():
                for q in p['spec']['data']['spec']['queries']:
                    assert 'node=~"${worker_host:regex}"' in q['spec']['query']['spec']['expr']
