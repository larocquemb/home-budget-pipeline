import importlib.util
from pathlib import Path

import pytest

from home_budget_pipeline.development_backend import bootstrap, validate_database_url, validate_endpoints, verify_database


def helper():
    path = Path(__file__).resolve().parents[1] / 'scripts/development_backend.py'
    import sys
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location('development_backend_helper', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def values():
    module = helper()
    return module.client_values({key: 'a' * 64 for key in module.SECRET_KEYS})


def test_generated_connections_are_development_only():
    configured = values()
    validate_endpoints(configured)
    assert '/home-budget-dev' in configured['RABBITMQ_URL']
    assert '/home-budget-test' in configured['TEST_RABBITMQ_URL']
    assert configured['NEO4J_URI'].endswith(':17687')


@pytest.mark.parametrize('url', [
    'postgresql://home_budget_dev:pass@postgres.brownrook.net:15432/home_budget_dev',
    'postgresql://home_budget_dev:pass@arsene.brownrook.net:5432/home_budget_dev',
    'postgresql://development_admin:pass@arsene.brownrook.net:15432/home_budget_dev',
    'postgresql://home_budget_dev:pass@arsene.brownrook.net:15432/home_budget',
    'postgresql://home_budget_test:pass@arsene.brownrook.net:15432/home_budget_test',
    'postgresql://home_budget_dev:pass@arsene.brownrook.net:15432/home_budget_dev?host=production',
])
def test_database_guard_rejects_production_admin_and_test_targets(url):
    with pytest.raises(ValueError):
        validate_database_url(url)


@pytest.mark.parametrize('key,value', [
    ('RABBITMQ_URL', 'amqp://receipts:pass@rabbitmq:5672/receipts'),
    ('TEST_RABBITMQ_URL', 'amqp://home_budget_test:pass@arsene.brownrook.net:15673/home-budget-dev'),
    ('NEO4J_URI', 'bolt://arsene.brownrook.net:7687'),
    ('HOME_BUDGET_ENVIRONMENT', 'production'),
])
def test_other_services_cannot_silently_select_production(key, value):
    configured = values()
    configured[key] = value
    with pytest.raises(ValueError):
        validate_endpoints(configured)


class Connection:
    def __init__(self, identity=('home_budget_dev', 'home_budget_dev', 'development'), existing=False):
        self.identity, self.existing, self.calls = identity, existing, []

    def execute(self, query):
        self.calls.append(query)
        return self

    def fetchone(self):
        return (self.existing,) if 'to_regnamespace' in self.calls[-1] else self.identity


def test_schema_reset_checks_connected_identity_before_any_drop(tmp_path):
    conn = Connection(('home_budget', 'home_budget_dev', None))
    with pytest.raises(ValueError):
        bootstrap(conn, tmp_path, reset=True, confirmation='home_budget_dev')
    assert not any('DROP ' in query for query in conn.calls)


def test_reset_requires_exact_confirmation_and_bootstrap_preserves_existing_data(tmp_path):
    for reset, confirmation in ((True, ''), (True, 'home_budget_test'), (False, '')):
        conn = Connection(existing=True)
        with pytest.raises(ValueError):
            bootstrap(conn, tmp_path, reset=reset, confirmation=confirmation)
        assert not any('DROP ' in query for query in conn.calls)


def test_confirmed_test_reset_uses_test_identity_and_ordered_bootstrap(tmp_path):
    conn = Connection(('home_budget_test', 'home_budget_test', 'development'))
    (tmp_path / '002.sql').write_text('SELECT 2;')
    (tmp_path / '001.sql').write_text('SELECT 1;')
    bootstrap(conn, tmp_path, test=True, reset=True, confirmation='home_budget_test')
    assert any('DROP SCHEMA' in query for query in conn.calls)
    assert conn.calls[-2:] == ['SELECT 1;', 'SELECT 2;']


def test_configuration_preserves_auth_settings_and_is_idempotent(tmp_path):
    module = helper()
    original = '# auth\nENTRA_CLIENT_SECRET=unchanged\nLEDGER_PROXY_SECRET=keep\nDATABASE_URL=old\n'
    result = module.update_env(original, values())
    assert 'ENTRA_CLIENT_SECRET=unchanged\nLEDGER_PROXY_SECRET=keep\n' in result
    assert 'DATABASE_URL=old' not in result
    assert module.update_env(result, values()) == result
    path = tmp_path / 'secret.yml'
    module.private_write(path, 'private', exclusive=True)
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        module.private_write(path, 'replacement', exclusive=True)
    assert path.read_text() == 'private'
