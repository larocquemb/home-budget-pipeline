#!/usr/bin/env python3
"""Replay a private graph export into isolated Arsene development Neo4j only."""
import argparse
import json
import os
from pathlib import Path
import resource
import sys
import time
from datetime import datetime, timezone

from postgres_credentials import read_env
from home_budget_pipeline.development_backend import validate_endpoints, verify_database
from home_budget_pipeline import receipt_graph as graph

ROOT = Path(__file__).resolve().parents[1]


def memory_enforcement():
    if sys.platform != 'linux':
        return {'memory_enforcement': 'native process observation; not a Linux cgroup limit'}
    for line in Path('/proc/self/cgroup').read_text().splitlines():
        if line.startswith('0::'):
            directory = Path('/sys/fs/cgroup') / line[3:].lstrip('/')
            maximum = (directory / 'memory.max').read_text().strip()
            peak = directory / 'memory.peak'
            return {'memory_enforcement': 'Linux cgroup v2' if maximum != 'max' else 'Linux cgroup v2; no memory cap',
                    'cgroup_memory_max_bytes': int(maximum) if maximum != 'max' else None,
                    'cgroup_memory_peak_bytes': int(peak.read_text()) if peak.exists() else None}
    return {'memory_enforcement': 'Linux memory cap unavailable; RSS observation only'}


def read_export(path):
    """Never buffer the receipt's many repeated collaboration JSON payloads."""
    with path.open() as source:
        first = json.loads(next(source))
        if first['kind'] != 'base':
            raise ValueError('Expected a saved read-only graph evidence export')
        data = first['data']
        def collaborations():
            for line in source:
                row = json.loads(line)
                if row['kind'] != 'collaboration':
                    raise ValueError('Unexpected evidence record')
                yield row['data']
        data['collaborations'] = collaborations()
        yield data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', required=True, type=Path)
    parser.add_argument('--env-file', type=Path, default=ROOT / '.env.dev')
    parser.add_argument('--run', default='')
    parser.add_argument('--output', type=Path, default=ROOT / '.validation/kan137/graph.json')
    args = parser.parse_args()
    values = read_env(args.env_file)
    validate_endpoints(values)
    import psycopg
    # Positively identify the isolated deployment before any graph writes.
    with psycopg.connect(values['DATABASE_URL'], connect_timeout=5) as conn:
        verify_database(conn)
    for key in ('NEO4J_URI', 'NEO4J_USER', 'NEO4J_PASSWORD', 'NEO4J_DATABASE'):
        os.environ[key] = values[key]
    started = time.monotonic()
    for evidence in read_export(args.evidence):
        sha = evidence['receipt']['source_sha256']
        built = graph.build(evidence)
    counts = {'nodes': len(built['nodes']), 'edges': len(built['edges'])}
    counts['catalogue_items'] = sum(n['kind'] == 'Item' for n in built['nodes'])
    counts['collaboration_runs'] = sum(n['kind'] == 'Collaboration' for n in built['nodes'])
    selected = next((n for n in built['nodes'] if n['kind'] == 'Collaboration'
                     and n['properties'].get('run_uuid') == args.run), None) if args.run else None
    if args.run and not selected:
        raise ValueError('Requested run is absent from this export')
    item_id = next((e['target'] for e in built['edges'] if selected and e['source'] == selected['id']
                    and e['kind'] == 'ANALYZES'), '')
    with graph.driver() as driver, driver.session(database='neo4j') as session:
        session.run('CREATE CONSTRAINT receipt_flow_id IF NOT EXISTS FOR (n:ReceiptFlowEntity) REQUIRE n.id IS UNIQUE').consume()
        session.run('CREATE CONSTRAINT receipt_snapshot_sha IF NOT EXISTS FOR (n:ReceiptSnapshot) REQUIRE n.sha IS UNIQUE').consume()
        session.execute_write(graph.write, sha, built, datetime.now(timezone.utc).isoformat(timespec='microseconds'))
    build_seconds = time.monotonic() - started
    run_id = selected['id'] if selected else ''
    del built, selected
    # Use the actual local web read path against development Neo4j. This is not
    # a substitute for the browser's authenticated end-to-end acceptance test.
    from home_budget_pipeline.web import receipt_graph as web
    started = time.monotonic()
    result = web.receipt(sha, perspective='story', offset=0, limit=100, item_id=item_id, run_id=run_id)
    lineage = web.receipt(sha, perspective='lineage', offset=0, limit=100)
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    report = {**counts, 'source_sha': sha, 'projection_seconds': build_seconds,
              'story_seconds': time.monotonic() - started, 'selected_nodes': len(result['nodes']),
              'selected_edges': len(result['edges']), 'items': len(result['items']), 'runs': len(result['runs']),
              'lineage_nodes': len(lineage['nodes']), 'lineage_total_nodes': lineage['total_nodes'],
              'peak_rss_bytes': peak if sys.platform == 'darwin' else peak * 1024,
              **memory_enforcement(),
              'projection_version': graph.VERSION, 'backend': 'isolated Arsene development Neo4j'}
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(args.output, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as output:
        os.fchmod(output.fileno(), 0o600)
        json.dump(report, output, indent=2)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'Development graph replay failed ({type(exc).__name__}); private details suppressed.', file=sys.stderr)
        raise SystemExit(2) from None
