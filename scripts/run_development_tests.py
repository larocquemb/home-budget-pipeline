#!/usr/bin/env python3
"""Run local development integration tests only against Arsene test services."""
import os
from pathlib import Path
import subprocess
import sys

from postgres_credentials import read_env
from home_budget_pipeline.development_backend import validate_endpoints, verify_database

ROOT = Path(__file__).resolve().parents[1]


def test_environment(values, inherited):
    if not values.get('TEST_DATABASE_URL'):
        raise ValueError('The isolated test database is retired; integration tests cannot use development or production data')
    validate_endpoints(values)
    env = {**inherited, **values}
    # Ambient libpq settings must not override the validated destination.
    for key in ('PGHOST', 'PGHOSTADDR', 'PGPORT', 'PGDATABASE', 'PGUSER',
                'PGPASSWORD', 'PGSERVICE', 'PGSERVICEFILE', 'PGOPTIONS'):
        env.pop(key, None)
    env['TEST_DATABASE_URL'] = values['TEST_DATABASE_URL']
    env['DATABASE_URL'] = values['TEST_DATABASE_URL']
    env['HOME_BUDGET_PG_DSN'] = values['TEST_DATABASE_URL']
    env['TEST_RABBITMQ_URL'] = values['TEST_RABBITMQ_URL']
    env['RABBITMQ_URL'] = values['TEST_RABBITMQ_URL']
    # Community Neo4j is shared with dev; integration tests need a separate graph.
    for key in ('TEST_NEO4J_URI', 'NEO4J_URI', 'NEO4J_USER', 'NEO4J_PASSWORD', 'NEO4J_DATABASE'):
        env.pop(key, None)
    return env


def main(argv):
    if not argv or argv[0] not in {'pytest', 'publish', 'check'}:
        raise ValueError('Use pytest [args], publish SOURCE_ROOT CACHE, or check')
    values = read_env(ROOT / '.env.dev')
    env = test_environment(values, os.environ)
    # Remove libpq environment overrides before validating the connected identity.
    for key in ('PGHOST', 'PGHOSTADDR', 'PGPORT', 'PGDATABASE', 'PGUSER',
                'PGPASSWORD', 'PGSERVICE', 'PGSERVICEFILE', 'PGOPTIONS'):
        os.environ.pop(key, None)
    import psycopg
    with psycopg.connect(values['TEST_DATABASE_URL'], connect_timeout=5) as conn:
        verify_database(conn, test=True)
    if argv[0] == 'check':
        print('Isolated Arsene test database identity verified; test broker vhost selected.')
        return 0
    if argv[0] == 'pytest':
        command = [sys.executable, '-m', 'pytest', *argv[1:]]
    else:
        if len(argv) != 3:
            raise ValueError('publish requires SOURCE_ROOT and CACHE')
        command = [str(ROOT / '.venv/bin/ledger'), 'receipts', 'process', argv[1], '--ocr-cache', argv[2]]
    return subprocess.run(command, cwd=ROOT, env=env).returncode


if __name__ == '__main__':
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Exception as exc:
        print(str(exc) if isinstance(exc, ValueError) else
              f'Development test setup failed ({type(exc).__name__}); connection details suppressed.', file=sys.stderr)
        raise SystemExit(2) from None
