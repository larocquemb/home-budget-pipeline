import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def setup_command(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / 'scripts'
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location('configure_receipt_postgres', scripts / 'configure_receipt_postgres.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reader_policy_keeps_native_auth_and_is_repeatable(setup_command):
    original = '# native\nlocal all all trust\nhost all all 127.0.0.1/32 trust\n'
    result = setup_command.reader_hba(original)
    assert result.endswith(original)
    assert 'hostssl home_budget grafana_receipt_reader 192.168.2.202/32 scram-sha-256' in result
    assert result.index('scram-sha-256') < result.index('0.0.0.0/0 reject')
    assert '::/0 reject' in result
    assert setup_command.reader_hba(result) == result


@pytest.mark.parametrize('dsn', [
    'postgresql://paul@127.0.0.1:5433/home_budget',
    'postgresql://paul@remote/home_budget',
    'postgresql://paul@127.0.0.1:5432/home_budget_test',
    'postgresql://paul@127.0.0.1:5432/home_budget?host=remote',
])
def test_setup_refuses_other_databases(setup_command, dsn):
    with pytest.raises(ValueError):
        setup_command.native_dsn(dsn)


def test_setup_accepts_native_database(setup_command):
    dsn = 'postgresql://paul@127.0.0.1:5432/home_budget'
    assert setup_command.native_dsn(dsn) == dsn
