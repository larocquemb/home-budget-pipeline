"""Log classification must preserve real INFO/ERROR outcomes in both runtimes."""
import json
from datetime import datetime

from home_budget_pipeline import receipt_graph, telemetry
from home_budget_pipeline.receipts import queue_ingest


def test_queue_json_retains_payload_and_adds_severity_without_mutating_input(capsys):
    payload = {'request_id': 'example-request', 'status': 'queued'}
    queue_ingest._print_json(payload)
    emitted = json.loads(capsys.readouterr().out)
    assert emitted['level'] == 'INFO'
    assert emitted['request_id'] == payload['request_id']
    assert datetime.fromisoformat(emitted['timestamp']).utcoffset().total_seconds() == 0
    assert payload == {'request_id': 'example-request', 'status': 'queued'}


def test_projection_summary_failure_is_error_not_unknown_or_info(monkeypatch, capsys):
    monkeypatch.setenv('DATABASE_URL', 'unused')
    monkeypatch.setattr(receipt_graph, 'project', lambda *a, **kw: {
        'projected': 0, 'failed': 1, 'scanned': 1, 'after': 'next'})
    assert receipt_graph.main([]) == 1
    emitted = json.loads(capsys.readouterr().out)
    assert emitted['level'] == 'ERROR' and emitted['failed'] == 1


def test_projection_exception_has_real_severity(monkeypatch, capsys):
    monkeypatch.setenv('DATABASE_URL', 'unused')
    def fail(*a, **kw): raise RuntimeError('example failure')
    monkeypatch.setattr(receipt_graph, 'project', fail)
    assert receipt_graph.main([]) == 1
    emitted = json.loads(capsys.readouterr().out)
    assert emitted['level'] == 'ERROR' and emitted['event'] == 'receipt_graph_projection_failed'
