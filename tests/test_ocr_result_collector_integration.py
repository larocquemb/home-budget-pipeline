"""PostgreSQL and RabbitMQ durability checks for the OCR result collector."""

import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from home_budget_pipeline.receipts import ocr_collector, ocr_results
from home_budget_pipeline.receipts import queue_ingest as queue
from test_ocr_results_collector import result_receipt


pytestmark = pytest.mark.integration


def _events(tmp_path):
    source = tmp_path / f"receipt-{uuid.uuid4().hex}.pdf"
    source.write_bytes(uuid.uuid4().bytes)
    receipt = result_receipt(source)
    receipt.ocr_run["run_uuid"] = str(uuid.uuid4())
    for metric in receipt.ocr_run["ocr_passes"]:
        metric["run_uuid"] = receipt.ocr_run["run_uuid"]
    cache = tmp_path / "cache"
    cache.mkdir(exist_ok=True)
    cache_path = cache / f"{source.name}.json"
    cache_path.write_text('{"pages":[]}', encoding="utf-8")
    return ocr_results.result_messages_for_receipt(
        receipt,
        cache_path=cache_path,
        cache_root=cache,
        trace_context={"traceparent": "00-" + uuid.uuid4().hex + "-" + uuid.uuid4().hex[:16] + "-01"},
    )


def _get_message(connection, channel, queue_name):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        method, properties, body = channel.basic_get(queue=queue_name, auto_ack=False)
        if method:
            return method, properties, body
        connection.sleep(0.05)
    pytest.fail(f"no delivery from {queue_name}")


@pytest.fixture
def result_broker():
    url = os.getenv("TEST_RABBITMQ_URL")
    if not url:
        pytest.skip("TEST_RABBITMQ_URL is not set")
    connection = queue.connect_broker(url)
    topology = ocr_collector.ResultTopology(
        "test.ocr-results." + uuid.uuid4().hex,
        retry_delay_ms=100,
    )
    channel = connection.channel()
    ocr_collector.declare_result_topology(channel, topology)
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


def test_collector_persists_run_pass_artifact_and_duplicate_idempotently(tmp_path):
    import psycopg

    run_event, pass_event = _events(tmp_path)
    with psycopg.connect(os.environ["TEST_DATABASE_URL"]) as conn:
        assert ocr_collector.persist_result(conn, run_event) == "succeeded"
        assert ocr_collector.persist_result(conn, pass_event) == "persisted"
        assert ocr_collector.persist_result(conn, run_event) == "duplicate"
        assert ocr_collector.persist_result(conn, pass_event) == "duplicate"

        assert conn.execute(
            "SELECT count(*) FROM budget.receipt_ocr_runs WHERE run_uuid = %s",
            (run_event.run_uuid,),
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT count(*) FROM budget.receipt_ocr_passes WHERE run_uuid = %s AND pass_id = 1",
            (run_event.run_uuid,),
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT count(*) FROM budget.ocr_result_events WHERE run_uuid = %s",
            (run_event.run_uuid,),
        ).fetchone() == (2,)
        assert conn.execute(
            "SELECT traceparent FROM budget.receipt_ocr_runs WHERE run_uuid = %s",
            (run_event.run_uuid,),
        ).fetchone() == (run_event.trace_context["traceparent"],)
        assert conn.execute(
            "SELECT uri, sha256, media_type, size_bytes FROM budget.receipt_ocr_artifacts WHERE run_uuid = %s",
            (run_event.run_uuid,),
        ).fetchone() == (
            run_event.artifacts[0].uri,
            run_event.artifacts[0].sha256,
            "application/json",
            run_event.artifacts[0].size_bytes,
        )
        assert conn.execute(
            "SELECT status FROM ingest.receipt_processing_status WHERE source_sha256 = %s",
            (run_event.source_sha256,),
        ).fetchone() == ("succeeded",)


def test_unacknowledged_result_is_persisted_by_replacement_without_duplicate_rows(
    result_broker, tmp_path,
):
    import psycopg

    event = _events(tmp_path)[0]
    connection, channel, topology = result_broker
    ocr_collector.publish_result(channel, topology.work, event)
    method, properties, body = _get_message(connection, channel, topology.work)
    with psycopg.connect(os.environ["TEST_DATABASE_URL"]) as conn:
        assert ocr_collector.persist_result(
            conn, ocr_results.OcrResultMessage.from_bytes(body),
        ) == "succeeded"

    connection.close()  # Commit succeeded; ACK was lost with this collector.
    replacement = queue.connect_broker(os.environ["TEST_RABBITMQ_URL"])
    try:
        replacement_channel = replacement.channel()
        method, properties, body = _get_message(
            replacement, replacement_channel, topology.work,
        )
        assert method.redelivered

        def persist(message):
            with psycopg.connect(os.environ["TEST_DATABASE_URL"]) as conn:
                return ocr_collector.persist_result(conn, message)

        ocr_collector.handle_result_delivery(
            replacement_channel,
            method.delivery_tag,
            body,
            persist,
            topology,
        )
        replacement.sleep(0.1)
        with psycopg.connect(os.environ["TEST_DATABASE_URL"]) as conn:
            assert conn.execute(
                "SELECT count(*) FROM budget.receipt_ocr_runs WHERE run_uuid = %s",
                (event.run_uuid,),
            ).fetchone() == (1,)
            assert conn.execute(
                "SELECT count(*) FROM budget.ocr_result_events WHERE message_id = %s",
                (event.message_id,),
            ).fetchone() == (1,)
    finally:
        replacement.close()


def test_interrupted_cache_worker_is_replaced_without_duplicate_ocr_or_rows(
    result_broker, tmp_path, monkeypatch,
):
    import psycopg

    source = tmp_path / f"receipt-{uuid.uuid4().hex}.pdf"
    source.write_bytes(uuid.uuid4().bytes)
    receipt = result_receipt(source)
    receipt.ocr_run["run_uuid"] = str(uuid.uuid4())
    for metric in receipt.ocr_run["ocr_passes"]:
        metric["run_uuid"] = receipt.ocr_run["run_uuid"]
    initial = ocr_results.result_messages_for_receipt(
        receipt,
        trace_context={"traceparent": "00-" + uuid.uuid4().hex + "-" + uuid.uuid4().hex[:16] + "-01"},
    )
    with psycopg.connect(os.environ["TEST_DATABASE_URL"]) as conn:
        for event in initial:
            ocr_collector.persist_result(conn, event)

    cache = tmp_path / "cache"
    cache.mkdir()
    cache_path = cache / f"{source.name}.json"
    replacement_receipt = result_receipt(source)
    replacement_receipt.items[0].item_name = "Must not replace extraction"
    replacement_receipt.ocr_run["run_uuid"] = str(uuid.uuid4())
    for metric in replacement_receipt.ocr_run["ocr_passes"]:
        metric["run_uuid"] = replacement_receipt.ocr_run["run_uuid"]
    refreshes = []

    def parse(*args, **kwargs):
        refreshes.append(args[4])
        if args[4]:
            cache_path.write_text('{"cache":"rebuilt"}', encoding="utf-8")
        return [replacement_receipt], 0 if args[4] else 1

    monkeypatch.setattr(queue.backlog, "parse_scans_parallel", parse)
    args = SimpleNamespace(receipt_root=str(tmp_path), ocr_cache=str(cache))
    request = queue.ReceiptMessage(
        receipt.source_sha256,
        source.name,
        version=3,
        request_id=str(uuid.uuid4()),
        batch_id=str(uuid.uuid4()),
        batch_index=1,
        batch_total=1,
    )
    connection, channel, result_topology = result_broker
    work_topology = queue.Topology("test.cache-worker." + uuid.uuid4().hex, retry_delay_ms=100)
    work_channel = connection.channel()
    queue.declare_topology(work_channel, work_topology)
    queue.publish_confirmed(
        work_channel,
        work_topology.work,
        request.to_bytes(),
        message_id=request.message_id,
        message_type=request.message_type,
    )
    method, _, body = _get_message(connection, work_channel, work_topology.work)
    status, events = queue.produce_result_messages(queue.ReceiptMessage.from_bytes(body), args)
    assert status == "cache_results_published"
    for event in events:
        ocr_collector.publish_result(channel, result_topology.work, event)

    connection.close()  # Results were confirmed; the source work ACK was lost.
    replacement = queue.connect_broker(os.environ["TEST_RABBITMQ_URL"])
    try:
        replacement_work_channel = replacement.channel()
        method, _, body = _get_message(
            replacement, replacement_work_channel, work_topology.work,
        )
        assert method.redelivered
        with ThreadPoolExecutor(max_workers=1) as executor:
            queue.handle_worker_delivery(
                replacement,
                replacement_work_channel,
                executor,
                method.delivery_tag,
                body,
                lambda message: queue.produce_result_messages(message, args),
                work_topology,
                result_topology,
            )

        replacement_result_channel = replacement.channel()

        def persist(message):
            with psycopg.connect(os.environ["TEST_DATABASE_URL"]) as conn:
                return ocr_collector.persist_result(conn, message)

        for _ in range(4):
            method, _, body = _get_message(
                replacement, replacement_result_channel, result_topology.work,
            )
            ocr_collector.handle_result_delivery(
                replacement_result_channel,
                method.delivery_tag,
                body,
                persist,
                result_topology,
            )

        assert refreshes == [True, False]
        with psycopg.connect(os.environ["TEST_DATABASE_URL"]) as conn:
            assert conn.execute(
                "SELECT count(*) FROM budget.receipt_cache_requests "
                "WHERE source_sha256 = %s AND request_id = %s AND completed_at IS NOT NULL",
                (request.source_sha256, request.request_id),
            ).fetchone() == (1,)
            assert conn.execute(
                "SELECT count(*) FROM budget.ocr_result_events WHERE run_uuid = %s",
                (replacement_receipt.ocr_run["run_uuid"],),
            ).fetchone() == (2,)
            assert conn.execute(
                "SELECT i.item_name FROM budget.expense_items i "
                "JOIN budget.receipt_evidence e ON e.expense_pk = i.expense_pk "
                "WHERE e.source_sha256 = %s",
                (request.source_sha256,),
            ).fetchall() == [("Tea",)]
    finally:
        if not replacement.is_open:
            replacement = queue.connect_broker(os.environ["TEST_RABBITMQ_URL"])
        cleanup = replacement.channel()
        for name in (work_topology.work, work_topology.retry, work_topology.dead):
            cleanup.queue_delete(queue=name)
            cleanup.exchange_delete(exchange=name)
        replacement.close()
