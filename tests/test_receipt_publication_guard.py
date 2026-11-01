"""Intake reservations fail closed and expose skipped in-flight jobs."""
from unittest.mock import MagicMock
from types import SimpleNamespace

import pytest
from home_budget_pipeline.receipts import queue_ingest as queue
from home_budget_pipeline.receipts.message import ReceiptMessage


@pytest.fixture
def publisher(tmp_path, monkeypatch):
    conn, broker = MagicMock(), MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = []
    message = ReceiptMessage('a'*64, 'receipt.pdf')
    monkeypatch.setattr(queue.backlog.scan, '_db_connect', lambda _: conn)
    monkeypatch.setattr(queue, 'connect_broker', lambda _: broker)
    monkeypatch.setattr(queue.receipt_lineage, 'start_batch', lambda *a: None)
    monkeypatch.setattr(queue, 'publication_plan', lambda *a, **k: ([message], {'discovered':1,'published':1}))
    args = SimpleNamespace(db_dsn='test', rabbitmq_url='test', receipt_root=tmp_path,
                           ingest_schema='ingest', ocr_cache=tmp_path, verbose=False)
    return args, conn, cursor, broker, message


def test_existing_inflight_job_opens_no_broker_and_reports_skip(publisher, monkeypatch, capsys):
    args, conn, cur, broker, message = publisher
    cur.fetchall.return_value = [(message.message_id, 'queued')]
    monkeypatch.setattr(queue, 'connect_broker', lambda _: pytest.fail('Already queued'))
    assert queue._publish_normal(args, queue.Topology()) == 0
    assert '"skipped_inflight": 1' in capsys.readouterr().out


def test_database_reservation_failure_does_not_publish(publisher):
    args, conn, cur, broker, message = publisher
    def execute(sql, *a):
        if sql.lstrip().startswith('INSERT'):
            raise RuntimeError('Database unavailable')
    cur.execute.side_effect = execute
    with pytest.raises(RuntimeError, match='Database unavailable'):
        queue._publish_normal(args, queue.Topology())
    broker.channel.return_value.basic_publish.assert_not_called()


def test_unknown_broker_outcome_retains_committed_reservation(publisher, monkeypatch):
    args, conn, cur, broker, message = publisher
    def fail(*a, **k):
        assert any('INSERT INTO lineage.receipt_events' in c.args[0] for c in cur.execute.call_args_list)
        assert conn.commit.call_count >= 2
        raise OSError('Confirmation lost')
    monkeypatch.setattr(queue, 'publish_confirmed', fail)
    with pytest.raises(RuntimeError, match='reservation retained'):
        queue._publish_normal(args, queue.Topology())
    assert not any(c.args[0].lstrip().startswith('DELETE') for c in cur.execute.call_args_list)
