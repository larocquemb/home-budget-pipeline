"""RabbitMQ consumer that durably persists versioned OCR result events."""

from __future__ import annotations

import json
import logging
import signal
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from .. import telemetry
from . import backlog_ingest as backlog
from .evidence import persist_ocr_pass, persist_ocr_run
from .ocr_results import (
    MAX_RESULT_ATTEMPTS,
    PASS_COMPLETED,
    RUN_COMPLETED,
    InvalidOcrResultMessage,
    OcrResultMessage,
    receipt_from_message,
)
from .parallel_ingest import persist_evidence_first
from .queue_ingest import connect_broker, process_with_heartbeats, publish_confirmed


LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResultTopology:
    prefix: str = "ocr.results.v1"
    retry_delay_ms: int = 60000

    @property
    def work(self) -> str:
        return self.prefix + ".work"

    @property
    def retry(self) -> str:
        return self.prefix + ".retry"

    @property
    def dead(self) -> str:
        return self.prefix + ".dead"


def declare_result_topology(channel, topology: ResultTopology) -> None:
    """Declare durable result, bounded-retry, and diagnostic DLQ queues."""
    for name in (topology.work, topology.retry, topology.dead):
        channel.exchange_declare(exchange=name, exchange_type="direct", durable=True)
    for name in (topology.dead, topology.work, topology.retry):
        arguments = {
            "x-queue-type": "quorum",
            "x-overflow": "reject-publish",
            "x-max-length": 100000,
        }
        if name != topology.dead:
            target = topology.work if name == topology.retry else topology.dead
            arguments.update({
                "x-dead-letter-exchange": target,
                "x-dead-letter-routing-key": "ocr-result",
                "x-dead-letter-strategy": "at-least-once",
            })
        if name == topology.work:
            arguments["x-delivery-limit"] = 20
        if name == topology.retry:
            arguments["x-message-ttl"] = topology.retry_delay_ms
        channel.queue_declare(queue=name, durable=True, arguments=arguments)
        channel.queue_bind(queue=name, exchange=name, routing_key="ocr-result")
    channel.confirm_delivery()


def publish_result(channel, exchange: str, message: OcrResultMessage) -> None:
    publish_confirmed(
        channel,
        exchange,
        message.to_bytes(),
        message_id=message.message_id,
        message_type=message.event_type,
        routing_key="ocr-result",
    )


def _already_persisted(conn, message_id: str, schema: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT 1 FROM {schema}.ocr_result_events WHERE message_id = %s",
            (message_id,),
        )
        return cur.fetchone() is not None


def _record_event(conn, message: OcrResultMessage, schema: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {schema}.ocr_result_events (
                message_id, schema_version, event_type, run_uuid, pass_id,
                source_sha256, source_reference, worker_identity, trace_context,
                occurred_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (message_id) DO NOTHING
            """,
            (
                message.message_id, message.schema_version, message.event_type,
                message.run_uuid, message.pass_id, message.source_sha256,
                message.source_reference, json.dumps(message.worker),
                json.dumps(message.trace_context), message.occurred_at,
            ),
        )


def _persist_artifacts(conn, message: OcrResultMessage, schema: str) -> None:
    with conn.cursor() as cur:
        for artifact in message.artifacts:
            cur.execute(
                f"""
                INSERT INTO {schema}.receipt_ocr_artifacts (
                    run_uuid, kind, uri, sha256, media_type, size_bytes
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (run_uuid, uri) DO UPDATE SET
                    kind = EXCLUDED.kind,
                    sha256 = EXCLUDED.sha256,
                    media_type = EXCLUDED.media_type,
                    size_bytes = EXCLUDED.size_bytes
                """,
                (
                    message.run_uuid, artifact.kind, artifact.uri,
                    artifact.sha256, artifact.media_type, artifact.size_bytes,
                ),
            )


def _annotate_run(conn, message: OcrResultMessage, schema: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {schema}.receipt_ocr_runs
               SET result_schema_version = %s,
                   traceparent = %s,
                   worker_identity = %s
             WHERE run_uuid = %s
            """,
            (
                message.schema_version,
                message.trace_context["traceparent"],
                json.dumps(message.worker),
                message.run_uuid,
            ),
        )


def _persist_cache_only(conn, message: OcrResultMessage, receipt, schema: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT id FROM {schema}.receipt_evidence WHERE source_sha256 = %s",
            (message.source_sha256,),
        )
        row = cur.fetchone()
    if row is None:
        raise InvalidOcrResultMessage("cache-only result has no existing receipt evidence")
    persist_ocr_run(conn, int(row[0]), receipt, schema)
    request_id = message.payload["request_id"]
    if request_id:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {schema}.receipt_cache_requests (
                    source_sha256, request_id, attempts, completed_at
                ) VALUES (%s, %s, 1, NOW())
                ON CONFLICT (source_sha256, request_id) DO UPDATE SET
                    completed_at = NOW()
                """,
                (message.source_sha256, request_id),
            )


def _persist_run(
    conn,
    message: OcrResultMessage,
    *,
    ingest_schema: str,
    budget_schema: str,
) -> str:
    receipt = receipt_from_message(message)
    candidate = backlog.ReceiptCandidate(Path(message.source_reference), message.source_sha256)
    mode = message.payload["persistence_mode"]
    with backlog.receipt_lock(conn, message.source_sha256):
        if _already_persisted(conn, message.message_id, budget_schema):
            conn.commit()
            return "duplicate"
        if mode == "cache_only":
            _persist_cache_only(conn, message, receipt, budget_schema)
            status = "cache_rebuilt"
        else:
            request_id = message.payload["request_id"]
            if mode == "reprocess" and not request_id:
                raise InvalidOcrResultMessage("reprocess result requires request_id")
            if request_id:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        INSERT INTO {budget_schema}.receipt_reprocess_requests (
                            source_sha256, request_id, attempts
                        ) VALUES (%s, %s, 1)
                        ON CONFLICT (source_sha256, request_id) DO NOTHING
                        """,
                        (message.source_sha256, request_id),
                    )
            backlog._mark_processing(conn, candidate, Path("."), ingest_schema)
            persist_evidence_first(
                conn,
                [receipt],
                budget_schema,
                replace_existing=mode == "reprocess",
            )
            status = "review_required" if receipt.extraction_status != "complete" else "succeeded"
            backlog._mark_completed(conn, candidate, Path("."), ingest_schema, status)
            if request_id:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        UPDATE {budget_schema}.receipt_reprocess_requests
                           SET completed_at = NOW()
                         WHERE source_sha256 = %s AND request_id = %s
                        """,
                        (message.source_sha256, request_id),
                    )
        _annotate_run(conn, message, budget_schema)
        _persist_artifacts(conn, message, budget_schema)
        _record_event(conn, message, budget_schema)
        conn.commit()
        return status


def _persist_pass(conn, message: OcrResultMessage, *, budget_schema: str) -> str:
    if _already_persisted(conn, message.message_id, budget_schema):
        conn.commit()
        return "duplicate"
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT 1 FROM {budget_schema}.receipt_ocr_runs WHERE run_uuid = %s",
            (message.run_uuid,),
        )
        if cur.fetchone() is None:
            raise RuntimeError("pass result arrived before its durable OCR run")
    persist_ocr_pass(conn, message.run_uuid, message.payload["pass"], budget_schema)
    _record_event(conn, message, budget_schema)
    conn.commit()
    return "persisted"


def persist_result(
    conn,
    message: OcrResultMessage,
    *,
    ingest_schema: str = "ingest",
    budget_schema: str = "budget",
) -> str:
    """Persist all required writes atomically before the caller ACKs."""
    backlog.validate_schema(ingest_schema)
    backlog.validate_schema(budget_schema)
    started = time.perf_counter()
    remote_context = telemetry.extract_trace_context(message.trace_context)
    try:
        with telemetry.span(
            "ocr.results.persist",
            {
                "messaging.system": "rabbitmq",
                "messaging.destination.name": "ocr.results.v1.work",
                "messaging.message.id": message.message_id,
                "ocr.result.event_type": message.event_type,
                "run_uuid": message.run_uuid,
                "pass_id": message.pass_id,
            },
            context=remote_context,
        ):
            if message.event_type == RUN_COMPLETED:
                outcome = _persist_run(
                    conn,
                    message,
                    ingest_schema=ingest_schema,
                    budget_schema=budget_schema,
                )
            elif message.event_type == PASS_COMPLETED:
                outcome = _persist_pass(conn, message, budget_schema=budget_schema)
            else:  # guarded by contract validation
                raise InvalidOcrResultMessage("unsupported event type")
    except Exception:
        conn.rollback()
        telemetry.record_collector_result(
            message.event_type, "failure", time.perf_counter() - started,
        )
        raise
    telemetry.record_collector_result(
        message.event_type, "success", time.perf_counter() - started,
    )
    return outcome


def handle_result_delivery(
    channel,
    delivery_tag: int,
    body: bytes,
    process,
    topology: ResultTopology,
) -> None:
    """ACK only after commit or a confirmed retry/dead-letter publication."""
    message = None
    try:
        message = OcrResultMessage.from_bytes(body)
        status = process(message)
    except Exception as exc:
        permanent = isinstance(exc, InvalidOcrResultMessage)
        retry = message is not None and not permanent and message.attempt < MAX_RESULT_ATTEMPTS
        target = topology.retry if retry else topology.dead
        next_body = message.next_attempt().to_bytes() if retry else body
        publish_confirmed(
            channel,
            target,
            next_body,
            message_id=message.message_id if message else None,
            error=type(exc).__name__,
            error_message=str(exc)[:512],
            message_type=message.event_type if message else "ocr.result.invalid",
            routing_key="ocr-result",
        )
        telemetry.record_collector_routing("retry" if retry else "dead")
        LOG.warning(
            "ocr_result=%s routed=%s error=%s",
            message.message_id if message else "invalid",
            target,
            type(exc).__name__,
        )
    else:
        LOG.info("ocr_result=%s status=%s", message.message_id, status)
    channel.basic_ack(delivery_tag=delivery_tag)


def consume_results(connection, channel, process, topology: ResultTopology, stop: Event) -> None:
    channel.basic_qos(prefetch_count=1)
    with ThreadPoolExecutor(max_workers=1) as executor:
        try:
            for method, properties, body in channel.consume(
                topology.work, auto_ack=False, inactivity_timeout=1,
            ):
                if stop.is_set():
                    break
                if method is None:
                    continue
                handle_result_delivery(
                    channel,
                    method.delivery_tag,
                    body,
                    lambda message: process_with_heartbeats(
                        connection, executor, process, message,
                    ),
                    topology,
                )
        finally:
            if channel.is_open:
                channel.cancel()


def run_collector(args) -> int:
    telemetry.configure_logging()
    telemetry.configure_telemetry()
    logging.getLogger("pika").setLevel(logging.WARNING)
    if not args.db_dsn:
        raise ValueError("Missing database DSN (--db-dsn, DATABASE_URL, or HOME_BUDGET_PG_DSN)")
    if not args.rabbitmq_url:
        raise ValueError("Missing RabbitMQ URL (--rabbitmq-url or RABBITMQ_URL)")
    topology = ResultTopology()
    connection = connect_broker(args.rabbitmq_url)
    try:
        channel = connection.channel()
        declare_result_topology(channel, topology)
        stop = Event()
        previous = {
            sig: signal.signal(sig, lambda signum, frame: stop.set())
            for sig in (signal.SIGINT, signal.SIGTERM)
        }
        try:
            def process(message):
                conn = backlog.scan._db_connect(args.db_dsn)
                try:
                    return persist_result(
                        conn,
                        message,
                        ingest_schema=args.ingest_schema,
                        budget_schema=args.budget_schema,
                    )
                finally:
                    conn.close()

            consume_results(connection, channel, process, topology, stop)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
    finally:
        if connection.is_open:
            connection.close()
        telemetry.shutdown_telemetry()
    return 0
