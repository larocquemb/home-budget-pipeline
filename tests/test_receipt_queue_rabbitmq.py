"""Live broker checks: confirmations, redelivery, delayed retry, and dead letters."""

import os
import time
import uuid
from dataclasses import replace
from threading import Event

import pytest

from home_budget_pipeline.receipts import queue_ingest as queue
from home_budget_pipeline.receipts.message import ReceiptMessage

pytestmark = pytest.mark.integration


@pytest.fixture
def broker():
    url = os.getenv("TEST_RABBITMQ_URL")
    if not url:
        pytest.skip("TEST_RABBITMQ_URL is not set")
    # When explicitly configured (CI), a missing client/broker is a failure.
    connection = queue.connect_broker(url)
    topology = queue.Topology("test." + uuid.uuid4().hex, retry_delay_ms=100)
    channel = connection.channel()
    queue.declare_topology(channel, topology)
    try:
        yield connection, channel, topology
    finally:
        if not connection.is_open:
            connection = queue.connect_broker(url)
        channel = connection.channel()
        for name in (topology.work, topology.retry, topology.dead):
            channel.queue_delete(queue=name)
            channel.exchange_delete(exchange=name)
        connection.close()


def get_message(connection, channel, name):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        method, properties, body = channel.basic_get(queue=name, auto_ack=False)
        if method:
            return method, properties, body
        connection.sleep(0.05)
    pytest.fail(f"no delivery from {name}")


def test_confirmed_publish_and_unacked_connection_loss_redelivers(broker):
    connection, channel, topology = broker
    message = ReceiptMessage("a" * 64, "receipt.pdf")
    queue.publish_confirmed(channel, topology.work, message.to_bytes(), message_id=message.message_id)
    method, properties, body = get_message(connection, channel, topology.work)
    assert properties.delivery_mode == 2
    connection.close()  # No ACK; replacement consumer must get the same job.
    replacement = queue.connect_broker(os.environ["TEST_RABBITMQ_URL"])
    try:
        second_channel = replacement.channel()
        method, properties, body = get_message(replacement, second_channel, topology.work)
        assert method.redelivered
        assert body == message.to_bytes()
        queue.handle_delivery(second_channel, method.delivery_tag, body, lambda msg: "skipped", topology)
        replacement.sleep(0.1)
        assert second_channel.basic_get(queue=topology.work)[0] is None
    finally:
        replacement.close()


@pytest.mark.parametrize("version", [1, 2, 3])
def test_retry_delay_and_bounded_dlq_preserve_identity(broker, version):
    connection, channel, topology = broker
    original = ReceiptMessage("b" * 64, "receipt.pdf")
    if version > 1:
        original = replace(original, version=version, request_id=str(uuid.uuid4()))
    queue.publish_confirmed(channel, topology.work, original.to_bytes(), message_id=original.message_id)
    def fail(message):
        raise RuntimeError("transient OCR error")
    for attempt in (1, 2, 3):
        method, properties, body = get_message(connection, channel, topology.work)
        assert ReceiptMessage.from_bytes(body) == replace(original, attempt=attempt)
        assert properties.message_id == original.message_id
        queue.handle_delivery(channel, method.delivery_tag, body, fail, topology)
    method, properties, body = get_message(connection, channel, topology.dead)
    assert ReceiptMessage.from_bytes(body).attempt == 3
    channel.basic_ack(method.delivery_tag)


def test_unroutable_publish_is_reported(broker):
    import pika

    connection, channel, topology = broker
    channel.queue_unbind(queue=topology.work, exchange=topology.work, routing_key="receipt")
    with pytest.raises(pika.exceptions.UnroutableError):
        queue.publish_confirmed(channel, topology.work, b"{}")


def test_consumer_uses_manual_ack_and_drains_on_stop(broker):
    connection, channel, topology = broker
    message = ReceiptMessage("c" * 64, "receipt.pdf")
    queue.publish_confirmed(channel, topology.work, message.to_bytes())
    stop = Event()
    def process(msg):
        stop.set()
        return "succeeded"
    queue.consume(connection, channel, process, topology, stop)
    assert channel.basic_get(queue=topology.work)[0] is None


@pytest.mark.parametrize("reprocess", [False, True])
def test_database_commit_followed_by_lost_ack_does_not_repeat_ocr_or_items(broker, tmp_path, monkeypatch, reprocess):
    import psycopg
    from types import SimpleNamespace
    from test_receipt_queue_postgres import receipt_for

    source = tmp_path / "receipt.pdf"
    source.write_bytes(uuid.uuid4().bytes)
    receipt = receipt_for(source)
    calls = []
    def parse(*args, **kwargs):
        calls.append(receipt.source_sha256)
        return [receipt]
    monkeypatch.setattr(queue.backlog, "parse_scans_parallel", parse)
    args = SimpleNamespace(
        receipt_root=str(tmp_path), db_dsn=os.environ["TEST_DATABASE_URL"],
        ocr_cache=str(tmp_path / "cache"), ingest_schema="ingest", budget_schema="budget",
    )
    connection, channel, topology = broker
    message = ReceiptMessage(receipt.source_sha256, source.name)
    if reprocess:
        # Start with a completed receipt; the request must force one new OCR run.
        assert queue.process_message(message, args) == "succeeded"
        calls.clear()
        message = replace(message, version=2, request_id=str(uuid.uuid4()))
    queue.publish_confirmed(channel, topology.work, message.to_bytes())
    method, properties, body = get_message(connection, channel, topology.work)
    assert queue.process_message(ReceiptMessage.from_bytes(body), args) == "succeeded"
    connection.close()  # Committed, but never ACKed.
    replacement = queue.connect_broker(os.environ["TEST_RABBITMQ_URL"])
    try:
        channel = replacement.channel()
        method, properties, body = get_message(replacement, channel, topology.work)
        assert method.redelivered
        queue.handle_delivery(
            channel, method.delivery_tag, body,
            lambda msg: queue.process_message(msg, args), topology,
        )
        assert calls == [receipt.source_sha256]
        with psycopg.connect(args.db_dsn) as conn:
            assert conn.execute(
                "SELECT count(*) FROM budget.expense_items i JOIN budget.receipt_evidence e "
                "ON e.expense_pk = i.expense_pk WHERE e.source_sha256 = %s", (receipt.source_sha256,),
            ).fetchone() == (1,)
            assert conn.execute(
                "SELECT attempts FROM ingest.receipt_processing_status WHERE source_sha256 = %s", (receipt.source_sha256,),
            ).fetchone() == (2 if reprocess else 1,)
    finally:
        replacement.close()


@pytest.mark.parametrize("initial_status", ["complete", "review"])
def test_scheduled_publication_reprocesses_deleted_cache_and_replaces_results(broker, tmp_path, monkeypatch, initial_status):
    import psycopg
    from types import SimpleNamespace
    from test_receipt_queue_postgres import receipt_for

    source = tmp_path / "2026-08-14" / "receipt.pdf"
    source.parent.mkdir()
    source.write_bytes(uuid.uuid4().bytes)
    reference = source.relative_to(tmp_path).as_posix()
    cache = tmp_path / "cache"
    cache_file = cache / (reference + ".json")
    receipt = receipt_for(source)
    receipt.source_reference = reference
    receipt.extraction_status = initial_status
    calls = []
    def parse(paths, root, workers, cache_dir, refresh):
        calls.append(refresh)
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text('{"rebuilt": true}')
        return [receipt]
    monkeypatch.setattr(queue.backlog, "parse_scans_parallel", parse)
    args = SimpleNamespace(
        receipt_root=str(tmp_path), db_dsn=os.environ["TEST_DATABASE_URL"],
        ocr_cache=str(cache), ingest_schema="ingest", budget_schema="budget",
    )
    normal = ReceiptMessage(receipt.source_sha256, reference)
    expected = "succeeded" if initial_status == "complete" else "review_required"
    assert queue.process_message(normal, args) == expected
    # The completed receipt is skipped while its cache exists.
    with psycopg.connect(args.db_dsn) as conn:
        assert queue.publication_plan(conn, tmp_path, cache_dir=cache)[0] == []
    cache_file.unlink()
    receipt.extraction_status = "complete"
    receipt.items[0].item_name = "Updated extraction"
    with psycopg.connect(args.db_dsn) as conn:
        messages, summary = queue.publication_plan(conn, tmp_path, cache_dir=cache)
        repeated, _ = queue.publication_plan(conn, tmp_path, cache_dir=cache)
    assert summary["cache_missing"] == 1
    assert messages == repeated
    request = messages[0]
    connection, channel, topology = broker
    queue.publish_confirmed(
        channel, topology.work, request.to_bytes(),
        message_id=request.message_id, message_type=request.message_type,
    )
    method, properties, body = get_message(connection, channel, topology.work)
    assert properties.type == "receipt.reprocess.v2"
    queue.handle_delivery(channel, method.delivery_tag, body, lambda m: queue.process_message(m, args), topology)
    assert cache_file.is_file()
    assert queue.process_message(request, args) == "skipped"
    assert calls == [False, True]
    with psycopg.connect(args.db_dsn) as conn:
        rows = conn.execute(
            "SELECT i.item_name FROM budget.expense_items i JOIN budget.receipt_evidence e "
            "ON e.expense_pk = i.expense_pk WHERE e.source_sha256 = %s", (receipt.source_sha256,),
        ).fetchall()
        assert rows == [("Updated extraction",)]
        assert queue.publication_plan(conn, tmp_path, cache_dir=cache)[0] == []
        # A later deletion must create a new request, not reuse the completed one.
        cache_file.unlink()
        later, _ = queue.publication_plan(conn, tmp_path, cache_dir=cache)
        assert later[0].request_id != request.request_id


def test_cache_rebuild_is_consumed_and_preserves_extraction_after_lost_ack(broker, tmp_path, monkeypatch):
    import psycopg
    from types import SimpleNamespace
    from test_receipt_queue_postgres import receipt_for

    source = tmp_path / "receipt.pdf"
    source.write_bytes(uuid.uuid4().bytes)
    receipt = receipt_for(source)
    calls = []
    cache_file = tmp_path / "cache" / "receipt.pdf.json"
    def parse(*args):
        calls.append(args[-1])
        cache_file.parent.mkdir(exist_ok=True)
        cache_file.write_text("cached")
        return [receipt]
    monkeypatch.setattr(queue.backlog, "parse_scans_parallel", parse)
    args = SimpleNamespace(receipt_root=str(tmp_path), db_dsn=os.environ["TEST_DATABASE_URL"],
                           ocr_cache=str(cache_file.parent), ingest_schema="ingest", budget_schema="budget")
    normal = ReceiptMessage(receipt.source_sha256, source.name)
    assert queue.process_message(normal, args) == "succeeded"
    with psycopg.connect(args.db_dsn) as conn:
        original_state = conn.execute("SELECT * FROM ingest.receipt_processing_status WHERE source_sha256 = %s", (receipt.source_sha256,)).fetchone()
    original_name = receipt.items[0].item_name
    receipt.items[0].item_name = "Must not replace extraction"
    cache_file.unlink()
    request = replace(normal, version=3, request_id=str(uuid.uuid4()))
    connection, channel, topology = broker
    queue.publish_confirmed(channel, topology.work, request.to_bytes(), message_id=request.message_id, message_type=request.message_type)
    method, properties, body = get_message(connection, channel, topology.work)
    assert properties.type == "receipt.cache-rebuild.v3"
    assert queue.process_message(ReceiptMessage.from_bytes(body), args) == "cache_rebuilt"
    connection.close()  # Completion committed, ACK lost.
    replacement = queue.connect_broker(os.environ["TEST_RABBITMQ_URL"])
    try:
        channel = replacement.channel()
        method, properties, body = get_message(replacement, channel, topology.work)
        assert method.redelivered
        queue.handle_delivery(channel, method.delivery_tag, body, lambda m: queue.process_message(m, args), topology)
        assert cache_file.is_file()
        assert calls == [False, True]
        with psycopg.connect(args.db_dsn) as conn:
            assert conn.execute("SELECT * FROM ingest.receipt_processing_status WHERE source_sha256 = %s", (receipt.source_sha256,)).fetchone() == original_state
            assert conn.execute("SELECT i.item_name FROM budget.expense_items i JOIN budget.receipt_evidence e ON e.expense_pk = i.expense_pk WHERE e.source_sha256 = %s", (receipt.source_sha256,)).fetchall() == [(original_name,)]
            assert conn.execute("SELECT attempts, completed_at IS NOT NULL FROM budget.receipt_cache_requests WHERE source_sha256 = %s AND request_id = %s", (receipt.source_sha256, request.request_id)).fetchone() == (1, True)
    finally:
        replacement.close()


def test_cache_request_attempt_budget_survives_fresh_deliveries(broker, tmp_path, monkeypatch):
    import psycopg
    from types import SimpleNamespace

    source = tmp_path / "receipt.pdf"
    source.write_bytes(uuid.uuid4().bytes)
    message = ReceiptMessage(queue.backlog.scan.sha256_file(source), source.name, version=3, request_id=str(uuid.uuid4()))
    args = SimpleNamespace(receipt_root=str(tmp_path), db_dsn=os.environ["TEST_DATABASE_URL"],
                           ocr_cache=str(tmp_path / "cache"), ingest_schema="ingest", budget_schema="budget")
    calls = []
    def fail(*args):
        calls.append(1)
        raise RuntimeError("OCR failed")
    monkeypatch.setattr(queue.backlog, "parse_scans_parallel", fail)
    for _ in range(3):
        with pytest.raises(RuntimeError, match="OCR failed"):
            queue.process_message(message, args)
    with pytest.raises(queue.backlog.RetryExhausted):
        queue.process_message(message, args)
    assert len(calls) == 3
    with psycopg.connect(args.db_dsn) as conn:
        assert conn.execute("SELECT count(*) FROM ingest.receipt_processing_status WHERE source_sha256 = %s", (message.source_sha256,)).fetchone() == (0,)
