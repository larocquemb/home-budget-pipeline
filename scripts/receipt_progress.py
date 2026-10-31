#!/usr/bin/env python3
"""Emit the same read-only progress snapshot for development and production."""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


def snapshot(environment="development"):
    import psycopg
    from psycopg.rows import dict_row
    import pika
    from home_budget_pipeline.cli_environment import read_environment
    from home_budget_pipeline.development_backend import validate_endpoints
    from home_budget_pipeline.receipt_progress import database_snapshot, queue_snapshot, reconcile_queue_snapshot
    if environment not in {'development', 'production'}:
        raise ValueError('Unsupported progress environment')
    values = read_environment(ROOT / ('.env.dev' if environment == 'development' else '.env.k3s'))
    if environment == 'development':
        validate_endpoints(values)
    else:
        from urllib.parse import urlsplit
        database = urlsplit(values['DATABASE_URL'])
        if not (database.hostname == 'postgres.brownrook.net' and (database.port or 5432) == 5432
                and database.path == '/home_budget' and database.username == 'home_budget'):
            raise ValueError('Unexpected production progress endpoints')
    with psycopg.connect(values['DATABASE_URL'], connect_timeout=3, row_factory=dict_row,
                         options='-c default_transaction_read_only=on -c statement_timeout=5000') as conn:
        # Identity guard uses tuple rows.
        with conn.cursor(row_factory=psycopg.rows.tuple_row) as cur:
            cur.execute("SELECT current_database(),current_user,current_setting('home_budget.environment',true)")
            identity = cur.fetchone()
            expected = 'home_budget_dev' if environment == 'development' else 'home_budget'
            if identity[:2] != (expected, expected) or (environment == 'development' and identity[2] != 'development'):
                raise ValueError('Unexpected progress database identity')
        result = database_snapshot(conn)
        result["environment"] = environment
    try:
        if environment == 'production':
            import base64
            import urllib.request
            user = values['RABBITMQ_DEFAULT_USER']
            password = values['RABBITMQ_DEFAULT_PASS']
            request = urllib.request.Request('https://rabbitmq.brownrook.net/api/queues/receipts')
            credentials = base64.b64encode(f'{user}:{password}'.encode()).decode()
            request.add_header('Authorization', 'Basic ' + credentials)
            with urllib.request.urlopen(request, timeout=5) as response:
                queues = {q['name']: q for q in json.load(response)}
            for name, queue in [('queue_waiting', 'receipts.v1.work'),
                                ('queue_retry', 'receipts.v1.retry'), ('queue_failed', 'receipts.v1.dead'),
                                ('collector_waiting', 'ocr.results.v1.work'), ('collector_failed', 'ocr.results.v1.dead')]:
                result[name] = queues[queue]['messages_ready']
            result['queue_available'] = 1
            return reconcile_queue_snapshot(result)
        params = pika.URLParameters(values['RABBITMQ_URL'])
        params.socket_timeout = 3
        params.stack_timeout = 5
        params.connection_attempts = 1
        broker = pika.BlockingConnection(params)
        try:
            result.update(queue_snapshot(broker), queue_available=1)
        finally:
            broker.close()
    except Exception as exc:
        result.update(queue_available=0, queue_error=type(exc).__name__)
    return reconcile_queue_snapshot(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--environment', choices=['development', 'production', 'both'], default='both')
    args = parser.parse_args()
    while True:
        environments = ['development', 'production'] if args.environment == 'both' else [args.environment]
        success = True
        for environment in environments:
            try:
                result = snapshot(environment)
                result['level'] = 'info'
            except Exception as exc:
                # No stale counts or connection URLs on failure.
                result = {'event': 'receipt_progress_error', 'level': 'error', 'environment': environment,
                          'error': type(exc).__name__}
                success = False
            print(json.dumps(result, sort_keys=True), flush=True)
        if args.once:
            return 0 if success else 1
        time.sleep(10)


if __name__ == '__main__':
    raise SystemExit(main())
