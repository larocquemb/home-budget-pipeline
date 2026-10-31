"""The development test runner must not reach application or production data."""
import importlib.util
import sys
from pathlib import Path

import pytest


def helper():
    path = Path(__file__).resolve().parents[1] / 'scripts/run_development_tests.py'
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location('development_test_runner', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runner_uses_test_database_and_broker_and_removes_ambient_overrides(arsene_development_environment):
    values = dict(arsene_development_environment)
    values['TEST_DATABASE_URL'] = values['DATABASE_URL'].replace('home_budget_dev', 'home_budget_test')
    env = helper().test_environment(values, {'PGHOSTADDR': '192.0.2.1', 'TEST_NEO4J_URI': 'bolt://example.invalid:7687'})
    assert env['DATABASE_URL'] == env['TEST_DATABASE_URL'] == values['TEST_DATABASE_URL']
    assert env['RABBITMQ_URL'] == env['TEST_RABBITMQ_URL'] == values['TEST_RABBITMQ_URL']
    assert 'PGHOSTADDR' not in env
    assert 'TEST_NEO4J_URI' not in env and 'NEO4J_URI' not in env


def test_runner_rejects_wrong_test_database(arsene_development_environment):
    values = dict(arsene_development_environment)
    values['TEST_DATABASE_URL'] = values['DATABASE_URL']
    with pytest.raises(ValueError):
        helper().test_environment(values, {})


def test_runner_refuses_retired_test_database(arsene_development_environment):
    values = dict(arsene_development_environment)
    values.pop('TEST_DATABASE_URL')
    with pytest.raises(ValueError, match='retired'):
        helper().test_environment(values, {'TEST_DATABASE_URL': 'ambient-value'})
