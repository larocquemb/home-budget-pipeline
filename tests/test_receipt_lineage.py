from unittest.mock import MagicMock

from home_budget_pipeline import receipt_lineage
from home_budget_pipeline.receipts.message import ReceiptMessage


def test_evidence_unavailability_does_not_fail_receipt_work(monkeypatch, caplog):
    import psycopg
    monkeypatch.setenv('DATABASE_URL', 'postgresql://invalid/')
    monkeypatch.setattr(psycopg, 'connect', MagicMock(side_effect=RuntimeError('do not log credentials')))
    receipt_lineage.record(ReceiptMessage('a'*64, 'receipt.pdf'), 'active', attempt_id='attempt')
    assert 'lineage_evidence_unavailable' in caplog.text
    assert 'do not log credentials' not in caplog.text


def test_evidence_records_delivery_and_worker_without_graph_dependency(monkeypatch):
    import psycopg
    connect = MagicMock()
    monkeypatch.setattr(psycopg, 'connect', connect)
    monkeypatch.setenv('DATABASE_URL', 'postgresql://test/')
    monkeypatch.setenv('K8S_NODE_NAME', 'longbow')
    receipt_lineage.record(ReceiptMessage('b'*64, 'receipt.pdf'), 'active', attempt_id='attempt')
    calls = connect.return_value.__enter__.return_value.execute.call_args_list
    payload = calls[-1].args[1][-1].obj
    assert payload['attempt_id'] == 'attempt'
    assert payload['worker_node'] == 'longbow'
    assert payload['status'] == 'active'


def test_completion_retains_actual_cache_use(monkeypatch):
    import psycopg
    connect = MagicMock()
    monkeypatch.setattr(psycopg, 'connect', connect)
    monkeypatch.setenv('DATABASE_URL', 'postgresql://test/')
    receipt_lineage.record(ReceiptMessage('b'*64, 'receipt.pdf'), 'results_published',
                           attempt_id='attempt', cache_hit=False)
    payload = connect.return_value.__enter__.return_value.execute.call_args_list[-1].args[1][-1].obj
    assert payload['cache_hit'] is False


def test_batch_start_is_authoritative_and_not_a_confirmed_member(monkeypatch, caplog):
    import logging
    import psycopg
    connect = MagicMock()
    monkeypatch.setattr(psycopg, 'connect', connect)
    monkeypatch.setenv('DATABASE_URL', 'postgresql://test/')
    batch = '740023b1-a078-4914-b074-81bd7129bb75'
    message = ReceiptMessage('b'*64, 'receipt.pdf')
    with caplog.at_level(logging.INFO):
        receipt_lineage.start_batch(message, batch, 151, 'receipts.v1.work')
    payload = connect.return_value.__enter__.return_value.execute.call_args_list[-1].args[1][-1].obj
    assert payload['batch_id'] == batch and payload['batch_total'] == 151
    assert payload['status'] == 'batch_started'
    assert payload['message_id'] is None and payload['request_id'] is None
    assert any(getattr(r, 'event', None) == 'batch_started' and r.batch_id == batch for r in caplog.records)
    import json
    from home_budget_pipeline.telemetry import JsonLogFormatter
    logged = next(r for r in caplog.records if getattr(r, 'event', None) == 'batch_started')
    serialized = json.loads(JsonLogFormatter().format(logged))
    assert serialized['batch_id'] == batch and serialized['batch_total'] == 151
