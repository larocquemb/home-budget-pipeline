"""Persist raw Brave search evidence; rescore it for each item at the caller."""
from contextvars import ContextVar
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os

ACTIVE_CACHE = ContextVar('brave_query_cache', default=None)
DDL = '''CREATE TABLE IF NOT EXISTS enrichment.brave_query_cache (
    cache_key TEXT PRIMARY KEY, request JSONB NOT NULL, results JSONB NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), expires_at TIMESTAMPTZ NOT NULL
)'''


def request_parameters(query):
    return {'endpoint': 'web/search', 'version': 1, 'q': query.strip(),
            'count': 10, 'country': 'ca', 'search_lang': 'en'}


def cache_key(parameters):
    return hashlib.sha256(json.dumps(parameters, sort_keys=True).encode()).hexdigest()


class BraveQueryCache:
    def __init__(self, dsn, *, refresh=False):
        self.dsn, self.refresh = dsn, refresh
        self.context, self.observations, self.memory = {}, [], {}
        self.hits = self.requests = 0
        days = os.getenv('BRAVE_CACHE_DAYS', '90')
        if not days.isdigit() or not 1 <= int(days) <= 3650:
            raise ValueError('BRAVE_CACHE_DAYS must be between 1 and 3650')
        self.positive_ttl = int(days) * 86400

    def __enter__(self):
        import psycopg
        from psycopg.rows import dict_row
        self.conn = psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row)
        try:
            # Existing deployments also need the additive cache table.
            self.conn.execute('SELECT pg_advisory_lock(78344001)')
            try:
                self.conn.execute(DDL)
            finally:
                self.conn.execute('SELECT pg_advisory_unlock(78344001)')
            self.token = ACTIVE_CACHE.set(self)
        except Exception:
            self.conn.close()
            raise
        return self

    def __exit__(self, *exc):
        ACTIVE_CACHE.reset(self.token)
        self.conn.close()

    def get(self, query, fetch):
        from psycopg.types.json import Jsonb
        parameters = request_parameters(query)
        key = cache_key(parameters)
        row = self.memory.get(key)
        if row is not None and row['expires_at'] <= datetime.now(timezone.utc):
            row = None
        status = 'memory_hit'
        if row is None:
            # Serialize concurrent misses for the same query across Jobs.
            lock = int(key[:16], 16)
            if lock >= 2**63:
                lock -= 2**64
            self.conn.execute('SELECT pg_advisory_lock(%s)', (lock,))
            try:
                row = None if self.refresh else self.conn.execute('''SELECT results,fetched_at,expires_at
                    FROM enrichment.brave_query_cache WHERE cache_key=%s AND expires_at>NOW()''', (key,)).fetchone()
                status = 'persistent_hit'
                if row is None:
                    self.requests += 1
                    results = fetch()
                    fetched_at = datetime.now(timezone.utc)
                    ttl = self.positive_ttl if results else 3600
                    self.conn.execute('''INSERT INTO enrichment.brave_query_cache
                        (cache_key,request,results,fetched_at,expires_at)
                        VALUES (%s,%s,%s,%s,%s + %s * interval '1 second')
                        ON CONFLICT(cache_key) DO UPDATE SET results=EXCLUDED.results,
                        request=EXCLUDED.request,fetched_at=EXCLUDED.fetched_at,expires_at=EXCLUDED.expires_at''',
                        (key, Jsonb(parameters), Jsonb(results), fetched_at, fetched_at, ttl))
                    row = {'results': results, 'fetched_at': fetched_at, 'expires_at': fetched_at + timedelta(seconds=ttl)}
                    status = 'refresh' if self.refresh else 'miss'
                self.memory[key] = row
            finally:
                self.conn.execute('SELECT pg_advisory_unlock(%s)', (lock,))
        if status.endswith('hit'):
            self.hits += 1
        record = {'query': query, 'cache_key': key, 'cache_status': status,
                  'fetched_at': row['fetched_at'].isoformat()}
        self.observations.append(record)
        print(json.dumps({'event': 'enrichment_brave_cache', 'level': 'INFO',
                          'timestamp': datetime.now(timezone.utc).isoformat(), **self.context, **record}), flush=True)
        return row['results']


def refresh_requested():
    value = os.getenv('ENRICHMENT_REFRESH', '0')
    if value not in {'0', '1'}:
        raise ValueError('ENRICHMENT_REFRESH must be 0 or 1')
    return value == '1'
