import json
import os
import uuid

import pytest

from home_budget_pipeline.receipt_graph import Graph, write

pytestmark = pytest.mark.integration


@pytest.fixture
def pg_conn():
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(os.environ['TEST_DATABASE_URL'], row_factory=dict_row) as conn:
        yield conn
        conn.rollback()


def test_real_postgres_attempt_history_is_authoritative(pg_conn):
    from psycopg.types.json import Jsonb
    event = uuid.uuid4()
    sha = uuid.uuid4().hex * 2
    try:
        pg_conn.execute('INSERT INTO lineage.receipt_events(id,source_sha256,source_reference,payload) VALUES (%s,%s,%s,%s)',
                        (event, sha, 'synthetic-receipt.pdf', Jsonb({'status': 'failed', 'attempt_id': str(event)})))
        row = pg_conn.execute('SELECT payload FROM lineage.receipt_events WHERE id=%s', (event,)).fetchone()
        payload = row['payload'] if isinstance(row, dict) else row[0]
        assert payload['status'] == 'failed'
    finally:
        pg_conn.rollback()


def test_real_neo4j_atomic_replay_and_out_of_order_snapshot():
    if not os.getenv('TEST_NEO4J_URI'):
        pytest.skip('TEST_NEO4J_URI not configured')
    from neo4j import GraphDatabase
    sha = uuid.uuid4().hex * 2
    graph = Graph()
    a = graph.node('Receipt', sha, 'Synthetic receipt')
    b = graph.node('Model', sha, 'Synthetic model')
    graph.edge(a, 'USES_MODEL', b)
    data = graph.export()
    with GraphDatabase.driver(os.environ['TEST_NEO4J_URI'], auth=('neo4j', 'test-graph-password')) as driver, driver.session(database='neo4j') as session:
        try:
            session.run('CREATE CONSTRAINT receipt_flow_id IF NOT EXISTS FOR (n:ReceiptFlowEntity) REQUIRE n.id IS UNIQUE').consume()
            session.run('CREATE CONSTRAINT receipt_snapshot_sha IF NOT EXISTS FOR (n:ReceiptSnapshot) REQUIRE n.sha IS UNIQUE').consume()
            for _ in range(2):
                assert session.execute_write(write, sha, data, '2026-10-04T15:00:00+00:00')
            assert session.run('MATCH (n:ReceiptFlowEntity) WHERE n.id IN $ids RETURN count(n) AS c', ids=[a, b]).single()['c'] == 2
            assert session.run('MATCH (a:ReceiptFlowEntity {id:$id})-[r]->() RETURN count(r) AS c', id=a).single()['c'] == 1
            assert not session.execute_write(write, sha, {'nodes': [], 'edges': []}, '2026-10-03T15:00:00+00:00')
            malformed = {**data, 'edges': [{**data['edges'][0], 'kind': 'INVALID; QUERY'}]}
            with pytest.raises(ValueError):
                session.execute_write(write, sha, malformed, '2026-10-05T15:00:00+00:00')
            row = session.run('MATCH (s:ReceiptSnapshot {sha:$sha}) RETURN s.graph_json AS graph,s.observed_at AS time', sha=sha).single()
            assert json.loads(row['graph']) == data
            assert row['time'] == '2026-10-04T15:00:00+00:00'
        finally:
            session.run('MATCH (n:ReceiptFlowEntity) WHERE n.id IN $ids DETACH DELETE n', ids=[a,b]).consume()
            session.run('MATCH (s:ReceiptSnapshot {sha:$sha}) DETACH DELETE s', sha=sha).consume()
