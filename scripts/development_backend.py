#!/usr/bin/env python3
"""Prepare private backend credentials/client settings or validate dev services."""
import argparse
import os
from pathlib import Path
import re
import secrets
import shlex
import subprocess
import sys

import yaml

from postgres_credentials import read_env
from home_budget_pipeline.development_backend import bootstrap, validate_endpoints, verify_database

ROOT = Path(__file__).resolve().parents[1]
SECRET_KEYS = ('postgres_admin', 'postgres_dev', 'postgres_test', 'rabbit_dev', 'rabbit_test', 'neo4j')


def private_write(path, text, *, exclusive=False):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, 'w') as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(text)


def client_values(secret):
    if set(secret) != set(SECRET_KEYS) or not all(re.fullmatch('[0-9a-f]{64}', str(v)) for v in secret.values()):
        raise ValueError('Use the generated 64-character hexadecimal credentials')
    host = 'arsene.brownrook.net'
    return {
        'HOME_BUDGET_ENVIRONMENT': 'development',
        'DATABASE_URL': f"postgresql://home_budget_dev:{secret['postgres_dev']}@{host}:15432/home_budget_dev",
        'TEST_DATABASE_URL': f"postgresql://home_budget_test:{secret['postgres_test']}@{host}:15432/home_budget_test",
        'RABBITMQ_URL': f"amqp://home_budget_dev:{secret['rabbit_dev']}@{host}:15673/home-budget-dev",
        'TEST_RABBITMQ_URL': f"amqp://home_budget_test:{secret['rabbit_test']}@{host}:15673/home-budget-test",
        'NEO4J_URI': f'bolt://{host}:17687',
        'NEO4J_USER': 'neo4j', 'NEO4J_PASSWORD': secret['neo4j'], 'NEO4J_DATABASE': 'neo4j',
        'OLLAMA_HOST': f'http://{host}:11436', 'OLLAMA_BASE_URL': f'http://{host}:11436',
        'OLLAMA_GPU_METRICS_URL': f'http://{host}:11435/gpu',
    }


def update_env(text, values):
    lines, pending = [], dict(values)
    for line in text.splitlines():
        match = re.match(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z_0-9]*)=', line)
        if match and match[1] in values:
            if match[1] in pending:
                lines.append(match[1] + '=' + shlex.quote(pending.pop(match[1])))
        else:
            lines.append(line)
    lines.extend(key + '=' + shlex.quote(value) for key, value in pending.items())
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('credentials', 'configure', 'check', 'bootstrap', 'reset'))
    parser.add_argument('--secrets-file', type=Path, default=ROOT / '.dev-backend/secrets.yml')
    parser.add_argument('--env-file', type=Path, default=ROOT / '.env.dev')
    parser.add_argument('--apply', action='store_true', help='Write connection changes; otherwise list changed keys only')
    parser.add_argument('--test', action='store_true', help='Select only the dedicated test database for schema operations')
    parser.add_argument('--confirm', default='')
    args = parser.parse_args()
    if args.command == 'credentials':
        private_write(args.secrets_file, yaml.safe_dump({'dev_backend_secrets': {
            key: secrets.token_hex(32) for key in SECRET_KEYS}}), exclusive=True)
        print('Created private development credentials; existing files are never overwritten.')
        return
    if args.command == 'configure':
        values = client_values(yaml.safe_load(args.secrets_file.read_text())['dev_backend_secrets'])
        validate_endpoints(values)
        if not args.apply:
            print('Would update keys: ' + ', '.join(values))
            return
        if not args.env_file.is_file():
            raise ValueError('Create the development env file with the existing HTTPS/Entra settings first')
        original = args.env_file.read_text()
        backup = args.env_file.with_name(args.env_file.name + '.before-arsene')
        if not backup.exists():
            private_write(backup, original, exclusive=True)
        private_write(args.env_file, update_env(original, values))
        # Password-free libpq profile for psql. pgAdmin uses the same host/port/user.
        private_write(ROOT / '.dev-backend/pg_service.conf',
            '[home-budget-dev]\nhost=arsene.brownrook.net\nport=15432\n'
            'dbname=home_budget_dev\nuser=home_budget_dev\n')
        print('Updated development connections; existing HTTPS/Entra settings preserved.')
        return
    values = read_env(args.env_file)
    validate_endpoints(values)
    import psycopg
    dsn = values['TEST_DATABASE_URL' if args.test else 'DATABASE_URL']
    if args.command in ('bootstrap', 'reset'):
        destination = ROOT / '.dev-backend/bootstrap'
        env = {**os.environ, 'PATH': str(ROOT / '.venv/bin') + os.pathsep + os.environ.get('PATH', '')}
        subprocess.run([str(ROOT / 'scripts/stage_db_bootstrap.sh'), str(destination)], check=True, env=env)
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            bootstrap(conn, destination, test=args.test, reset=args.command == 'reset', confirmation=args.confirm)
        print('Development schema operation committed.')
        return
    for test in (False, True):
        with psycopg.connect(values['TEST_DATABASE_URL' if test else 'DATABASE_URL'], connect_timeout=5) as conn:
            verify_database(conn, test=test)
    import pika
    for key in ('RABBITMQ_URL', 'TEST_RABBITMQ_URL'):
        params = pika.URLParameters(values[key])
        params.socket_timeout = 5
        params.connection_attempts = 1
        params.stack_timeout = 10
        connection = pika.BlockingConnection(params)
        connection.close()
    from neo4j import GraphDatabase
    with GraphDatabase.driver(values['NEO4J_URI'], auth=(values['NEO4J_USER'], values['NEO4J_PASSWORD']), connection_timeout=5) as driver:
        driver.verify_connectivity()
    print('Development/test PostgreSQL identities, RabbitMQ vhosts and development Neo4j are reachable.')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, FileExistsError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None
    except Exception as exc:
        print(f'Development backend operation failed ({type(exc).__name__}); connection secrets suppressed.', file=sys.stderr)
        raise SystemExit(2) from None
