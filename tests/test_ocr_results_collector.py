import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from home_budget_pipeline.receipts import ingest, ocr_collector, ocr_results
from home_budget_pipeline.receipts import queue_ingest as queue
from home_budget_pipeline.receipts.message import ReceiptMessage


RUN_UUID = "11111111-1111-1111-1111-111111111111"


def result_receipt(path: Path) -> ingest.ScannedReceipt:
    receipt = ingest.ScannedReceipt(
        path=str(path),
        source_reference=path.name,
        source_sha256=queue.backlog.scan.sha256_file(path),
        merchant="Test Shop",
        transaction_date="2026-09-16",
        receipt_id="r-1",
        subtotal=9.0,
        tax=1.0,
        total=10.0,
        payment_method="credit",
        card_last4="1234",
        items=[ingest.ScannedItem("Tea", 10.0)],
        text="Tea 10.00\nTOTAL 10.00",
        extraction_confidence=0.95,
        extraction_status="complete",
    )
    receipt.transaction_datetime = "2026-09-16T12:30:00"
    receipt.ocr_run = {
        "run_uuid": RUN_UUID,
        "cache_version": 13,
        "processed_at": "2026-09-16T17:30:00+00:00",
        "processing_seconds": 0.4,
        "timings": {"text_extraction_seconds": 0.3},
        "ocr_passes": [{
            "schema_version": 1,
            "run_uuid": RUN_UUID,
            "pass_id": 1,
            "page_number": 1,
            "engine": "tesseract",
            "engine_type": "traditional_ocr",
            "dpi": 300,
            "psm": "6",
            "variant": "raw",
            "seconds": 0.3,
            "status": "success",
            "error_type": None,
            "line_count": 2,
            "character_count": 22,
            "structural_score": 12,
            "summary_score": 100,
            "valid_timestamp": True,
            "selected_base": True,
            "consensus_line_coverage": 2,
            "consensus_coverage_ratio": 1.0,
            "quality": {"line_count": 2},
            "engine_options": {"psm": "6"},
            "usage": {},
            "provenance": {},
            "text": "alternate full OCR text",
        }],
    }
    return receipt


def test_versioned_run_and_pass_contract_round_trip_with_artifact(tmp_path):
    source = tmp_path / "receipt.pdf"
    source.write_bytes(b"receipt")
    cache = tmp_path / "cache"
    cache.mkdir()
    cache_path = cache / "receipt.pdf.json"
    cache_path.write_text('{"large":"artifact"}', encoding="utf-8")

    events = ocr_results.result_messages_for_receipt(
        result_receipt(source),
        cache_path=cache_path,
        cache_root=cache,
        trace_context={"traceparent": "00-" + "a" * 32 + "-" + "b" * 16 + "-01"},
    )

    assert [event.event_type for event in events] == [
        ocr_results.RUN_COMPLETED,
        ocr_results.PASS_COMPLETED,
    ]
    assert events[0].pass_id is None
    assert events[1].pass_id == 1
    assert events[0].source_sha256 == events[1].source_sha256
    assert events[0].worker == events[1].worker
    assert events[0].trace_context == events[1].trace_context
    assert events[0].artifacts[0].uri.endswith("/receipt.pdf.json")
    assert events[0].artifacts[0].size_bytes == cache_path.stat().st_size
    assert "text" not in events[1].payload["pass"]
    assert events[0].payload["receipt"]["text"].endswith("TOTAL 10.00")
    assert [ocr_results.OcrResultMessage.from_bytes(event.to_bytes()) for event in events] == events


def test_configured_object_store_is_written_before_event_publication(tmp_path, monkeypatch):
    source = tmp_path / "receipt.pdf"
    source.write_bytes(b"receipt")
    cache = tmp_path / "cache"
    cache.mkdir()
    cache_path = cache / "receipt.pdf.json"
    cache_path.write_bytes(b'{"large":"artifact"}')
    calls = []
    client = SimpleNamespace(put_object=lambda **kwargs: calls.append(kwargs))
    monkeypatch.setitem(
        sys.modules,
        "boto3",
        SimpleNamespace(client=lambda **kwargs: client),
    )
    monkeypatch.setenv("OCR_ARTIFACT_S3_BUCKET", "ledger-artifacts")
    monkeypatch.setenv("OCR_ARTIFACT_S3_PREFIX", "receipts/ocr")
    monkeypatch.setenv("OCR_ARTIFACT_S3_ENDPOINT", "https://objects.example")
    monkeypatch.setenv("OCR_ARTIFACT_S3_REGION", "ca-central-1")

    artifact = ocr_results.artifact_reference(cache_path, cache)

    assert artifact.uri == "s3://ledger-artifacts/receipts/ocr/receipt.pdf.json"
    assert calls == [{
        "Bucket": "ledger-artifacts",
        "Key": "receipts/ocr/receipt.pdf.json",
        "Body": b'{"large":"artifact"}',
        "ContentType": "application/json",
        "Metadata": {"sha256": artifact.sha256},
    }]


@pytest.mark.parametrize("mutation", [
    lambda value: value.update(schema_version=2),
    lambda value: value.update(run_uuid="bad"),
    lambda value: value.update(source_reference="../escape.pdf"),
    lambda value: value.update(trace_context={}),
    lambda value: value.update(pass_id=4),
])
def test_result_contract_rejects_invalid_or_inconsistent_messages(tmp_path, mutation):
    source = tmp_path / "receipt.pdf"
    source.write_bytes(b"receipt")
    event = ocr_results.result_messages_for_receipt(
        result_receipt(source),
        trace_context={"traceparent": "00-" + "a" * 32 + "-" + "b" * 16 + "-01"},
    )[1]
    value = json.loads(event.to_bytes())
    mutation(value)
    with pytest.raises(ocr_results.InvalidOcrResultMessage):
        ocr_results.OcrResultMessage.from_bytes(json.dumps(value).encode())


def test_result_topology_is_quorum_bounded_retry_and_dlq():
    declarations = []
    channel = SimpleNamespace(**{
        name: lambda _name=name, **kwargs: declarations.append((_name, kwargs))
        for name in ("exchange_declare", "queue_declare", "queue_bind", "confirm_delivery")
    })
    topology = ocr_collector.ResultTopology()
    ocr_collector.declare_result_topology(channel, topology)
    queues = {kwargs["queue"]: kwargs for method, kwargs in declarations if method == "queue_declare"}
    assert queues[topology.work]["arguments"]["x-queue-type"] == "quorum"
    assert queues[topology.retry]["arguments"]["x-message-ttl"] == 60000
    assert queues[topology.retry]["arguments"]["x-dead-letter-exchange"] == topology.work
    assert queues[topology.work]["arguments"]["x-dead-letter-exchange"] == topology.dead


@pytest.fixture
def channel(monkeypatch):
    monkeypatch.setitem(sys.modules, "pika", SimpleNamespace(BasicProperties=SimpleNamespace))
    events = []
    return SimpleNamespace(
        events=events,
        basic_publish=lambda **kwargs: events.append(("publish", kwargs)),
        basic_ack=lambda **kwargs: events.append(("ack", kwargs)),
    )


def test_collector_ack_follows_commit_and_retry_publish_precedes_ack(tmp_path, channel):
    source = tmp_path / "receipt.pdf"
    source.write_bytes(b"receipt")
    event = ocr_results.result_messages_for_receipt(
        result_receipt(source),
        trace_context={"traceparent": "00-" + "a" * 32 + "-" + "b" * 16 + "-01"},
    )[0]
    topology = ocr_collector.ResultTopology()

    def commit(message):
        channel.events.append(("commit", message.message_id))
        return "succeeded"

    ocr_collector.handle_result_delivery(channel, 7, event.to_bytes(), commit, topology)
    assert [item[0] for item in channel.events] == ["commit", "ack"]

    channel.events.clear()
    ocr_collector.handle_result_delivery(
        channel,
        8,
        event.to_bytes(),
        lambda message: (_ for _ in ()).throw(RuntimeError("database unavailable")),
        topology,
    )
    assert [item[0] for item in channel.events] == ["publish", "ack"]
    publication = channel.events[0][1]
    assert publication["exchange"] == topology.retry
    retried = ocr_results.OcrResultMessage.from_bytes(publication["body"])
    assert retried.attempt == 2
    assert publication["properties"].headers == {
        "error_type": "RuntimeError",
        "error_message": "database unavailable",
    }


def test_ocr_worker_produces_results_without_opening_database(tmp_path, monkeypatch):
    source = tmp_path / "receipt.pdf"
    source.write_bytes(b"receipt")
    receipt = result_receipt(source)
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "receipt.pdf.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(queue.backlog, "parse_scans_parallel", lambda *args, **kwargs: ([receipt], 0))
    monkeypatch.setattr(
        queue.backlog.scan,
        "_db_connect",
        lambda *args, **kwargs: pytest.fail("OCR worker opened PostgreSQL"),
    )
    args = SimpleNamespace(receipt_root=str(tmp_path), ocr_cache=str(cache))

    status, events = queue.produce_result_messages(
        ReceiptMessage(receipt.source_sha256, source.name), args,
    )

    assert status == "results_published"
    assert len(events) == 2
    assert events[0].payload["persistence_mode"] == "normal"


def test_cli_exposes_dedicated_collector(monkeypatch):
    from home_budget_pipeline.cli import build_parser

    monkeypatch.setenv("DATABASE_URL", "dbname=test")
    monkeypatch.setenv("RABBITMQ_URL", "amqp://example/")
    args = build_parser().parse_args(["receipts", "collect"])
    assert args.handler is ocr_collector.run_collector
    assert args.db_dsn == "dbname=test"
