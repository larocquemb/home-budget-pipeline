import json
from unittest.mock import MagicMock

import pytest

from home_budget_pipeline import cli
from home_budget_pipeline.receipts import backlog_ingest as backlog


REFERENCE = "2026-08-14/receipts_20260814_0001.pdf"
SHA = "a" * 64


def connection(*, matches=((SHA,),), lock=True, state=("processing", 5)):
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = list(matches)
    cursor.fetchone.side_effect = [(lock,), state]
    return conn, cursor


@pytest.mark.parametrize("status", ["processing", "failed"])
def test_reset_targets_one_unfinished_hash_under_shared_lock(status):
    conn, cursor = connection(state=(status, 5))
    result = backlog.reset_receipt_for_retry(conn, REFERENCE)
    assert result == {
        "source_reference": REFERENCE, "source_sha256": SHA,
        "previous_status": status, "previous_attempts": 5,
        "attempts": 0, "retry_ready": True,
    }
    calls = cursor.execute.call_args_list
    assert calls[0].args[1] == (REFERENCE,)
    assert "pg_try_advisory_xact_lock" in calls[1].args[0]
    assert calls[1].args[1] == (f"home-budget-receipt:{SHA}",)
    assert "SELECT status, attempts" in calls[2].args[0]
    assert "FOR UPDATE" in calls[2].args[0]
    assert "UPDATE ingest.receipt_processing_status" in calls[3].args[0]
    assert "attempts = 0" in calls[3].args[0]
    assert calls[3].args[1] == (SHA,)
    assert len(calls) == 4
    conn.commit.assert_called_once()
    conn.rollback.assert_not_called()


@pytest.mark.parametrize("kwargs,error", [
    ({"matches": ()}, "no receipt found"),
    ({"matches": ((SHA,), ("b" * 64,))}, "multiple receipt hashes"),
    ({"lock": False}, "currently being processed"),
    ({"state": None}, "no processing attempts"),
    ({"state": ("succeeded", 5)}, "already completed"),
    ({"state": ("review_required", 5)}, "already completed"),
])
def test_retry_refusals_preserve_state_and_release_transaction(kwargs, error):
    conn, cursor = connection(**kwargs)
    with pytest.raises((ValueError, RuntimeError), match=error):
        backlog.reset_receipt_for_retry(conn, REFERENCE)
    assert not any(call.args[0].startswith("UPDATE") for call in cursor.execute.call_args_list)
    conn.commit.assert_not_called()
    conn.rollback.assert_called_once()


def test_retry_does_not_report_success_after_commit_failure():
    conn, _ = connection()
    conn.commit.side_effect = RuntimeError("commit failed")
    with pytest.raises(RuntimeError, match="commit failed"):
        backlog.reset_receipt_for_retry(conn, REFERENCE)
    conn.rollback.assert_called_once()


def test_schema_validation_and_parameterized_source_reference():
    conn, cursor = connection()
    with pytest.raises(ValueError, match="invalid database schema"):
        backlog.reset_receipt_for_retry(conn, REFERENCE, ingest_schema="ingest; DROP SCHEMA budget")
    cursor.execute.assert_not_called()
    reference = "receipt's scan.pdf"
    backlog.reset_receipt_for_retry(conn, reference, ingest_schema="ingest_blue")
    assert "FROM ingest_blue.receipts" in cursor.execute.call_args_list[0].args[0]
    assert cursor.execute.call_args_list[0].args[1] == (reference,)


def test_retry_cli_resets_without_broker_connection(monkeypatch, capsys):
    from home_budget_pipeline.receipts import queue_ingest

    conn, _ = connection()
    monkeypatch.setenv("DATABASE_URL", "dbname=test")
    monkeypatch.delenv("RABBITMQ_URL", raising=False)
    monkeypatch.setattr(cli.scan, "_db_connect", lambda dsn: conn)
    monkeypatch.setattr(queue_ingest, "connect_broker", lambda *a: pytest.fail("retry must not connect to RabbitMQ"))
    assert cli.main(["receipts", "retry", REFERENCE]) == 0
    assert json.loads(capsys.readouterr().out)["retry_ready"] is True
    conn.close.assert_called_once()


def test_retry_cli_reports_busy_receipt_and_closes_connection(monkeypatch, capsys):
    conn, _ = connection(lock=False)
    monkeypatch.setattr(cli.scan, "_db_connect", lambda dsn: conn)
    assert cli.main(["receipts", "retry", REFERENCE, "--db-dsn", "dbname=test"]) == 1
    output = capsys.readouterr()
    assert "currently being processed" in output.err
    assert not output.out
    conn.close.assert_called_once()


def test_retry_cli_requires_database_configuration(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("HOME_BUDGET_PG_DSN", raising=False)
    monkeypatch.setattr(cli.scan, "_db_connect", lambda *a: pytest.fail("should not connect"))
    assert cli.main(["receipts", "retry", REFERENCE]) == 1
    assert "set -a; source .env.dev; set +a" in capsys.readouterr().err
