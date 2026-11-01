import json
from unittest.mock import MagicMock

import pytest

from home_budget_pipeline import cli
from home_budget_pipeline.receipts import queue_ingest as queue
from home_budget_pipeline.receipts.message import ReceiptMessage

REFERENCE = "2026-08-14/receipts_20260814_0001.pdf"
REQUEST_ID = "740023b1-a078-4914-b074-81bd7129bb75"


@pytest.fixture
def receipt_env(tmp_path, monkeypatch):
    root = tmp_path / "inbox"
    path = root / REFERENCE
    path.parent.mkdir(parents=True)
    path.write_bytes(b"selected receipt")
    (path.parent / "other.pdf").write_bytes(b"unrelated receipt")
    monkeypatch.setenv("RECEIPT_SOURCE_ROOT", str(root))
    monkeypatch.setenv("RABBITMQ_URL", "amqp://localhost/")
    monkeypatch.setattr(cli.scan, "_db_connect", lambda *a: pytest.fail("publisher opened database"))
    monkeypatch.setattr(queue.backlog, "process_candidate", lambda *a, **kw: pytest.fail("publisher ran OCR"))
    connection = MagicMock()
    connect = MagicMock(return_value=connection)
    monkeypatch.setattr(queue, "connect_broker", connect)
    return root, path, connection, connect


def test_reprocess_publishes_one_confirmed_persistent_request(receipt_env, capsys):
    _, path, connection, connect = receipt_env
    assert cli.main(["receipts", "reprocess", REFERENCE, "--request-id", REQUEST_ID, "--verbose"]) == 0
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["status"] == "queued"
    assert result["request_id"] == REQUEST_ID
    assert result["queue"] == "receipts.v1.work"
    assert result["source_reference"] == REFERENCE
    assert "Publishing reprocess request" in output.err
    channel = connection.channel.return_value
    channel.basic_publish.assert_called_once()
    publication = channel.basic_publish.call_args.kwargs
    message = ReceiptMessage.from_bytes(publication["body"])
    assert message == ReceiptMessage(cli.scan.sha256_file(path), REFERENCE, version=2, request_id=REQUEST_ID)
    assert publication["properties"].message_id == message.message_id
    assert publication["properties"].type == "receipt.reprocess.v2"
    assert publication["properties"].delivery_mode == 2
    assert publication["mandatory"] is True
    calls = [call[0] for call in channel.mock_calls]
    assert calls.index("confirm_delivery") < calls.index("basic_publish")
    connect.assert_called_once_with("amqp://localhost/")
    connection.close.assert_called_once()


def test_separate_invocations_generate_distinct_requests(receipt_env, capsys):
    ids = []
    for _ in range(2):
        assert cli.main(["receipts", "reprocess", REFERENCE]) == 0
        ids.append(json.loads(capsys.readouterr().out)["request_id"])
    assert ids[0] != ids[1]


@pytest.mark.parametrize("reference", ["../outside.pdf", "/tmp/outside.pdf", "./receipt.pdf", "a//b.pdf", "a\\b.pdf", "missing.pdf", "2026-08-14"])
def test_reprocess_rejects_invalid_sources_before_publication(receipt_env, capsys, reference):
    *_, connect = receipt_env
    assert cli.main(["receipts", "reprocess", reference]) == 1
    assert "Cannot queue reprocess request" in capsys.readouterr().err
    connect.assert_not_called()


def test_reprocess_rejects_symlink_outside_inbox(receipt_env, tmp_path):
    root, _, _, connect = receipt_env
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"outside")
    (root / "link.pdf").symlink_to(outside)
    assert cli.main(["receipts", "reprocess", "link.pdf"]) == 1
    connect.assert_not_called()


def test_reprocess_requires_broker_configuration(receipt_env, monkeypatch, capsys):
    *_, connect = receipt_env
    monkeypatch.delenv("RABBITMQ_URL")
    assert cli.main(["receipts", "reprocess", REFERENCE]) == 1
    assert "RABBITMQ_URL" in capsys.readouterr().err
    connect.assert_not_called()


def test_reprocess_rejects_invalid_request_id(receipt_env):
    *_, connect = receipt_env
    assert cli.main(["receipts", "reprocess", REFERENCE, "--request-id", "bad"]) == 1
    connect.assert_not_called()


def test_unconfirmed_publication_reports_no_success_and_can_be_retried(receipt_env, capsys):
    from pika.exceptions import NackError

    _, _, connection, _ = receipt_env
    channel = connection.channel.return_value
    channel.basic_publish.side_effect = NackError([])
    command = ["receipts", "reprocess", REFERENCE, "--request-id", REQUEST_ID]
    assert cli.main(command) == 1
    output = capsys.readouterr()
    assert not output.out
    assert "NackError" in output.err
    assert f"--request-id {REQUEST_ID}" in output.err
    first_body = channel.basic_publish.call_args.kwargs["body"]
    connection.close.assert_called_once()
    channel.basic_publish.side_effect = None
    assert cli.main(command) == 0
    assert channel.basic_publish.call_args.kwargs["body"] == first_body


def test_cached_reprocess_publishes_extraction_directly_to_collector(receipt_env, tmp_path, capsys):
    from home_budget_pipeline.receipts.ocr_results import OcrResultMessage
    _, path, connection, _ = receipt_env
    cache = tmp_path / 'cache'
    target = cache / (REFERENCE + '.json')
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({
        'metadata': {'source_sha256': cli.scan.sha256_file(path), 'cache_version': 1,
                     'run_uuid': '11111111-1111-4111-8111-111111111111', 'ocr_passes': []},
        'page_text': ['SYNTHETIC MARKET\n2026-10-25\nMilk $4.00\nTOTAL $4.00'],
    }))
    assert cli.main(['receipts', 'reprocess', REFERENCE, '--use-ocr-cache',
                     '--ocr-cache', str(cache), '--request-id', REQUEST_ID]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['ocr_executed'] is False
    publication = connection.channel.return_value.basic_publish.call_args.kwargs
    assert publication['exchange'] == 'ocr.results.v1.work'
    event = OcrResultMessage.from_bytes(publication['body'])
    assert event.payload['receipt']['merchant'] == 'SYNTHETIC MARKET'
    assert event.payload['request_id'] == REQUEST_ID
    assert event.payload['persistence_mode'] == 'reprocess'


def test_cached_reprocess_fails_before_broker_when_cache_missing(receipt_env, tmp_path):
    *_, connect = receipt_env
    assert cli.main(['receipts', 'reprocess', REFERENCE, '--use-ocr-cache',
                     '--ocr-cache', str(tmp_path / 'missing')]) == 1
    connect.assert_not_called()
