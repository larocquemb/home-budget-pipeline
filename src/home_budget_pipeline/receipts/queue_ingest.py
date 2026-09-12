"""Optional RabbitMQ transport around the existing receipt processor."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event
from uuid import uuid4

from . import backlog_ingest as backlog
from .message import MAX_ATTEMPTS, InvalidReceiptMessage, ReceiptMessage

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class Topology:
    prefix: str = "receipts.v1"
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


def declare_topology(channel, topology: Topology) -> None:
    """Quorum queues retain retry/dead-letter transfers until confirmed."""
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
                "x-dead-letter-routing-key": "receipt",
                "x-dead-letter-strategy": "at-least-once",
            })
        if name == topology.work:
            # Also bound repeated worker crashes, which do not advance the body.
            arguments["x-delivery-limit"] = 20
        if name == topology.retry:
            arguments["x-message-ttl"] = topology.retry_delay_ms
        channel.queue_declare(queue=name, durable=True, arguments=arguments)
        channel.queue_bind(queue=name, exchange=name, routing_key="receipt")
    channel.confirm_delivery()


def connect_broker(url: str):
    import pika

    if not url:
        raise ValueError("RABBITMQ_URL or --rabbitmq-url is required")
    parameters = pika.URLParameters(url)
    parameters.heartbeat = 60
    parameters.blocked_connection_timeout = 60
    parameters.socket_timeout = 10
    parameters.stack_timeout = 30
    return pika.BlockingConnection(parameters)


def publish_confirmed(channel, exchange: str, body: bytes, *, message_id: str | None = None, error: str | None = None, message_type: str = "receipt.process.v1") -> None:
    import pika

    channel.basic_publish(
        exchange=exchange,
        routing_key="receipt",
        body=body,
        mandatory=True,
        properties=pika.BasicProperties(
            content_type="application/json",
            content_encoding="utf-8",
            delivery_mode=2,
            message_id=message_id,
            type=message_type,
            headers={"error_type": error} if error else {},
        ),
    )


def publication_plan(conn, root: Path, ingest_schema: str = "ingest") -> tuple[list[ReceiptMessage], dict[str, int]]:
    """Rediscovery may publish duplicates; exhausted hashes require operator replay."""
    if not root.is_dir():
        raise ValueError("receipt root must be an existing directory")
    plan = backlog.plan_unprocessed_receipts(conn, root, ingest_schema=ingest_schema)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT source_sha256 FROM {ingest_schema}.receipt_processing_status WHERE attempts >= %s",
            (MAX_ATTEMPTS,),
        )
        exhausted = {row[0] for row in cur.fetchall()}
    conn.commit()
    messages = {}
    for candidate in plan.pending:
        if candidate.source_sha256 in exhausted:
            continue
        reference = candidate.path.relative_to(root).as_posix()
        message = ReceiptMessage(candidate.source_sha256, reference)
        message.resolve_source(root)
        messages.setdefault(message.message_id, message)
    return list(messages.values()), {
        "discovered": len(plan.discovered),
        "skipped": len(plan.skipped),
        "exhausted": sum(c.source_sha256 in exhausted for c in plan.pending),
        "published": len(messages),
    }


def publish_reprocess(args) -> int:
    """Publish one confirmed refresh request; OCR and DB work belong to consumers."""
    from pika.exceptions import AMQPError

    request_id = args.request_id or str(uuid4())
    try:
        if not args.rabbitmq_url:
            raise ValueError("RABBITMQ_URL or --rabbitmq-url is required")
        root = Path(args.receipt_root).expanduser().resolve()
        # Validate the reference before reading or hashing the selected source.
        selection = ReceiptMessage("0" * 64, args.source_reference, version=2, request_id=request_id)
        selection.resolve_source(root)
        path = root / selection.source_reference
        if not path.is_file():
            raise ValueError(f"Receipt not found: {selection.source_reference}")
        if path.suffix.lower() not in backlog.scan.SUPPORTED_EXTS:
            raise ValueError(f"Unsupported receipt file: {selection.source_reference}")
        message = replace(selection, source_sha256=backlog.scan.sha256_file(path))
        if args.verbose:
            print(f"Publishing reprocess request {request_id} for {message.source_reference}", file=sys.stderr, flush=True)
        connection = connect_broker(args.rabbitmq_url)
        try:
            channel = connection.channel()
            topology = Topology()
            declare_topology(channel, topology)
            publish_confirmed(
                channel, topology.work, message.to_bytes(),
                message_id=message.message_id, message_type=message.message_type,
            )
        finally:
            if connection.is_open:
                connection.close()
    except (ValueError, RuntimeError, OSError, AMQPError) as exc:
        # Connection errors can contain URLs; report their type without credentials.
        detail = str(exc) if isinstance(exc, InvalidReceiptMessage) or type(exc) is ValueError else type(exc).__name__
        print(f"Cannot queue reprocess request {request_id}: {detail}", file=sys.stderr)
        print(f"If publication was uncertain, retry with --request-id {request_id}.", file=sys.stderr)
        return 1
    print(json.dumps({
        "source_reference": message.source_reference,
        "source_sha256": message.source_sha256,
        "request_id": request_id,
        "queue": topology.work,
        "status": "queued",
    }, indent=2))
    return 0


def process_message(message: ReceiptMessage, args) -> str:
    root = Path(args.receipt_root).expanduser().resolve()
    message.resolve_source(root)
    path = root / message.source_reference
    conn = backlog.scan._db_connect(args.db_dsn)
    try:
        return backlog.process_candidate(
            conn, backlog.ReceiptCandidate(path, message.source_sha256),
            root, Path(args.ocr_cache).expanduser().resolve(),
            ingest_schema=args.ingest_schema,
            budget_schema=args.budget_schema,
            max_attempts=MAX_ATTEMPTS,
            verify_source=True,
            refresh_ocr_cache=message.request_id is not None,
            reprocess_request_id=message.request_id,
            progress=lambda text: LOG.info("receipt=%s %s", message.message_id, text),
        )
    finally:
        conn.close()


def handle_delivery(channel, delivery_tag: int, body: bytes, process, topology: Topology) -> None:
    """ACK only after DB success or a confirmed retry/DLQ publication.

    If the publish/ACK fails, let the connection close and RabbitMQ redeliver.
    The original remains unacknowledged; duplicate transfers are safe.
    """
    message = None
    try:
        message = ReceiptMessage.from_bytes(body)
        status = process(message)
    except Exception as exc:
        permanent = isinstance(exc, (InvalidReceiptMessage, backlog.RetryExhausted))
        retry = message is not None and not permanent and message.attempt < MAX_ATTEMPTS
        target = topology.retry if retry else topology.dead
        next_body = replace(message, attempt=message.attempt + 1).to_bytes() if retry else body
        publish_confirmed(
            channel, target, next_body,
            message_id=message.message_id if message else None,
            error=type(exc).__name__,
            message_type=message.message_type if message else "receipt.process.v1",
        )
        LOG.warning("receipt=%s routed=%s error=%s", message.message_id if message else "invalid", target, type(exc).__name__)
    else:
        LOG.info("receipt=%s status=%s", message.message_id, status)
    channel.basic_ack(delivery_tag=delivery_tag)


def process_with_heartbeats(connection, executor, process, message):
    """Keep AMQP I/O on its owning thread while OCR/DB work runs elsewhere."""
    future = executor.submit(process, message)
    while not future.done():
        connection.process_data_events(time_limit=1)
    return future.result()


def consume(connection, channel, process, topology: Topology, stop: Event) -> None:
    channel.basic_qos(prefetch_count=1)
    # The executor never accesses the channel or connection. One receipt per pod.
    with ThreadPoolExecutor(max_workers=1) as executor:
        try:
            for method, properties, body in channel.consume(topology.work, auto_ack=False, inactivity_timeout=1):
                if stop.is_set():
                    break
                if method is None:
                    continue
                handle_delivery(
                    channel, method.delivery_tag, body,
                    lambda message: process_with_heartbeats(connection, executor, process, message),
                    topology,
                )
        finally:
            if channel.is_open:
                channel.cancel()


def add_arguments(parser, mode: str) -> None:
    parser.set_defaults(queue_mode=mode, handler=run)
    parser.add_argument("receipt_root", nargs="?", default=backlog._default_path(
        "receipts/raw/scanned/inbox", "/data/receipts/raw/scanned/inbox", "RECEIPT_SOURCE_ROOT",
    ))
    parser.add_argument("--db-dsn", default=os.getenv("DATABASE_URL") or os.getenv("HOME_BUDGET_PG_DSN", ""))
    parser.add_argument("--rabbitmq-url", default=os.getenv("RABBITMQ_URL", ""))
    parser.add_argument("--ingest-schema", default="ingest")
    parser.add_argument("--budget-schema", default="budget")
    parser.add_argument("--ocr-cache", default=backlog._default_path(
        "receipts/derived/ocr-cache", "/data/receipts/derived/ocr-cache", "HOME_BUDGET_OCR_CACHE",
    ))


def run(args) -> int:
    logging.basicConfig(level=logging.INFO)
    # Keep the CLI focused on receipt outcomes. Pika's connection workflow logs
    # every socket and AMQP shutdown transition at INFO, which obscures them.
    logging.getLogger("pika").setLevel(logging.WARNING)
    backlog.validate_schema(args.ingest_schema)
    backlog.validate_schema(args.budget_schema)
    missing = []
    if not args.db_dsn:
        missing.append("database DSN (--db-dsn, DATABASE_URL, or HOME_BUDGET_PG_DSN)")
    if not args.rabbitmq_url:
        missing.append("RabbitMQ URL (--rabbitmq-url or RABBITMQ_URL)")
    if missing:
        raise ValueError(
            "Missing " + "; ".join(missing)
            + ". When loading .env.dev, export its variables: set -a; source .env.dev; set +a"
        )
    topology = Topology()
    if args.queue_mode == "publish":
        conn = backlog.scan._db_connect(args.db_dsn)
        try:
            messages, summary = publication_plan(conn, Path(args.receipt_root).expanduser().resolve(), args.ingest_schema)
        finally:
            conn.close()
    connection = connect_broker(args.rabbitmq_url)
    try:
        channel = connection.channel()
        declare_topology(channel, topology)
        if args.queue_mode == "publish":
            for message in messages:
                publish_confirmed(channel, topology.work, message.to_bytes(), message_id=message.message_id)
            print(json.dumps(summary, indent=2))
        else:
            stop = Event()
            previous = {sig: signal.signal(sig, lambda signum, frame: stop.set()) for sig in (signal.SIGINT, signal.SIGTERM)}
            try:
                consume(connection, channel, lambda message: process_message(message, args), topology, stop)
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
    finally:
        if connection.is_open:
            connection.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(required=True)
    for mode in ("publish", "consume"):
        add_arguments(commands.add_parser(mode), mode)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
