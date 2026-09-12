"""Receipt command entry points must publish, even when run inside a worker pod."""
import json
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from home_budget_pipeline import cli
from home_budget_pipeline.receipts import queue_ingest as queue
from home_budget_pipeline.receipts.message import ReceiptMessage


@pytest.fixture
def queued_cli(tmp_path, monkeypatch):
    source = tmp_path / "receipt.pdf"
    source.write_bytes(b"test source")
    monkeypatch.setenv("RECEIPT_SOURCE_ROOT", str(tmp_path))
    monkeypatch.setenv("RABBITMQ_URL", "amqp://localhost/")
    broker = MagicMock()
    monkeypatch.setattr(queue, "connect_broker", lambda url: broker)
    for name in ("process_backlog", "process_candidate", "parse_scans_parallel"):
        monkeypatch.setattr(queue.backlog, name, lambda *a, **kw: pytest.fail("command bypassed RabbitMQ"))
    return source, broker


@pytest.mark.parametrize("command,version", [
    (["ocr-cache", "rebuild"], 3),
    (["receipts", "process", "--refresh-ocr-cache"], 2),
    (["receipts", "reprocess", "receipt.pdf"], 2),
])
def test_manual_commands_publish_without_processing(queued_cli, monkeypatch, capsys, command, version):
    source, broker = queued_cli
    monkeypatch.setattr(queue.backlog.scan, "_db_connect", lambda *a: pytest.fail("batch publisher opened database"))
    assert cli.main([*command, "--verbose"]) == 0
    publication = broker.channel.return_value.basic_publish.call_args.kwargs
    message = ReceiptMessage.from_bytes(publication["body"])
    assert message.version == version
    assert message.source_sha256 == queue.backlog.scan.sha256_file(source)
    assert publication["properties"].type == message.message_type
    assert publication["properties"].delivery_mode == 2
    assert json.loads(capsys.readouterr().out)["status"] == "queued"
    broker.channel.return_value.confirm_delivery.assert_called_once()


@pytest.mark.parametrize("command", [["ocr-cache", "rebuild"], ["receipts", "process", "--refresh-ocr-cache"], ["receipts", "reprocess", "receipt.pdf"]])
def test_broker_failure_never_falls_back_to_local_ocr(queued_cli, monkeypatch, capsys, command):
    monkeypatch.setattr(queue, "connect_broker", MagicMock(side_effect=OSError("broker unavailable")))
    assert cli.main(command) == 1
    assert not capsys.readouterr().out


def test_batch_retry_reuses_each_request_and_partial_failure_is_reported(queued_cli, capsys):
    source, broker = queued_cli
    (source.parent / "second.pdf").write_bytes(b"second")
    batch = "740023b1-a078-4914-b074-81bd7129bb75"
    publish = broker.channel.return_value.basic_publish
    publish.side_effect = [None, OSError("lost confirmation")]
    command = ["ocr-cache", "rebuild", "--request-id", batch]
    assert cli.main(command) == 1
    initial = [call.kwargs["body"] for call in publish.call_args_list]
    output = capsys.readouterr()
    assert "1 publication(s) confirmed" in output.err
    assert batch in output.err
    assert not output.out
    publish.reset_mock(side_effect=True)
    assert cli.main(command) == 0
    assert [call.kwargs["body"] for call in publish.call_args_list] == initial


def test_process_backlog_entry_points_publish(queued_cli, monkeypatch):
    source, broker = queued_cli
    monkeypatch.setenv("DATABASE_URL", "dbname=test")
    conn = MagicMock()
    monkeypatch.setattr(queue.backlog.scan, "_db_connect", lambda *a: conn)
    candidate = queue.backlog.ReceiptCandidate(source, queue.backlog.scan.sha256_file(source))
    monkeypatch.setattr(queue.backlog, "plan_unprocessed_receipts", lambda *a, **kw: queue.backlog.DiscoveryPlan((candidate,), (candidate,), ()))
    conn.cursor.return_value.__enter__.return_value.fetchall.return_value = []
    assert cli.main(["receipts", "process"]) == 0
    monkeypatch.setattr("sys.argv", ["home-budget-process-receipts"])
    assert queue.backlog.main() == 0
    from home_budget_pipeline.receipts import parallel_ingest
    assert parallel_ingest.main() == 0
    assert broker.channel.return_value.basic_publish.call_count == 3


def test_cache_contract_round_trip_and_retry_identity():
    message = ReceiptMessage("a" * 64, "receipt.pdf", version=3, request_id="740023b1-a078-4914-b074-81bd7129bb75")
    assert ReceiptMessage.from_bytes(message.to_bytes()) == message
    assert replace(message, attempt=2).message_id == message.message_id
    assert replace(message, version=2).message_id != message.message_id


def test_empty_cache_batch_reports_no_publications(queued_cli, capsys):
    source, broker = queued_cli
    source.unlink()
    assert cli.main(["ocr-cache", "rebuild", "--verbose"]) == 2
    assert json.loads(capsys.readouterr().out)["published"] == 0
    broker.channel.return_value.basic_publish.assert_not_called()
