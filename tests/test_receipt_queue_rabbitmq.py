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


def test_retry_delay_and_bounded_dlq_preserve_identity(broker):
    connection, channel, topology = broker
    original = ReceiptMessage("b" * 64, "receipt.pdf")
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


def test_database_commit_followed_by_lost_ack_does_not_repeat_ocr_or_items(broker, tmp_path, monkeypatch):
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
            ).fetchone() == (1,)
    finally:
        replacement.close()
