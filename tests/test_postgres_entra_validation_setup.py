"""Guard the boundary between isolated Entra tests and existing applications."""
import importlib.util
from pathlib import Path
import sys

import pytest


@pytest.fixture
def setup_module():
    path = Path(__file__).resolve().parents[1] / 'scripts/prepare_postgres_entra_validation.py'
    spec = importlib.util.spec_from_file_location('entra_setup', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_preview_never_calls_azure(setup_module, monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['prepare'])
    monkeypatch.setattr(setup_module, 'az', lambda *args: pytest.fail('Preview called Azure'))
    setup_module.main()
    assert 'Preview only' in capsys.readouterr().out


def test_wrong_tenant_stops_before_writes(setup_module, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, 'argv', ['prepare', '--apply'])
    monkeypatch.setattr(setup_module, 'ROOT', tmp_path)
    calls = []

    def fake_az(*args):
        calls.append(args)
        return {'tenantId': 'wrong-tenant'}

    monkeypatch.setattr(setup_module, 'az', fake_az)
    with pytest.raises(ValueError, match='Wrong Azure tenant'):
        setup_module.main()
    assert calls == [('account', 'show')]
    assert not (tmp_path / '.validation').exists()


def test_wrong_user_stops_before_writes(setup_module, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, 'argv', ['prepare', '--apply'])
    monkeypatch.setattr(setup_module, 'ROOT', tmp_path)
    calls = []

    def fake_az(*args):
        calls.append(args)
        return {'tenantId': setup_module.TENANT} if args == ('account', 'show') else {'id': 'wrong-user'}

    monkeypatch.setattr(setup_module, 'az', fake_az)
    with pytest.raises(ValueError, match='Wrong signed-in user'):
        setup_module.main()
    assert all('rest' not in command for command in calls)
    assert not (tmp_path / '.validation').exists()


def test_foreign_registration_stops_before_patch(setup_module, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, 'argv', ['prepare', '--apply'])
    monkeypatch.setattr(setup_module, 'ROOT', tmp_path)
    directory = tmp_path / '.validation/kan137/entra'
    directory.mkdir(parents=True)
    setup_module.save(directory / 'registrations.json', {
        'tenant': setup_module.TENANT, 'user_oid': setup_module.USER,
        'marker': 'test-marker', 'scope_id': 'scope',
        'resource': {'id': 'existing-app', 'appId': setup_module.LEDGER}})
    calls = []

    def fake_az(*args):
        calls.append(args)
        if args == ('account', 'show'):
            return {'tenantId': setup_module.TENANT}
        if args == ('ad', 'signed-in-user', 'show'):
            return {'id': setup_module.USER}
        return {'appId': setup_module.LEDGER,
                'notes': 'Brown Rook KAN-137 isolated PostgreSQL validation test-marker resource'}

    monkeypatch.setattr(setup_module, 'az', fake_az)
    with pytest.raises(ValueError, match='Refusing to modify'):
        setup_module.main()
    assert all('PATCH' not in command and 'POST' not in command for command in calls)
