"""RabbitMQ dispatch and consumption for receipt processing and cache rebuilds."""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import signal
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from .. import telemetry
from . import backlog_ingest as backlog
from .message import MAX_ATTEMPTS, InvalidReceiptMessage, ReceiptMessage
from .parallel_ingest import _cache_path, has_ocr_cache

LOG = logging.getLogger(__name__)


def _print_json(payload: object) -> None:
    """Emit one JSON object per stdout line for container log collectors."""
    print(json.dumps(payload, sort_keys=True), flush=True)


def _cache_rebuild_marker(cache_dir: Path, message: ReceiptMessage) -> Path:
    if message.batch_id:
        return cache_dir / ".rebuild-batches" / message.batch_id / f"{message.request_id}.json"
    return cache_dir / ".rebuild-requests" / message.source_sha256 / f"{message.request_id}.json"


@contextmanager
def cache_rebuild_lock(cache_dir: Path, source_sha256: str):
    """Serialize cache writes for one receipt across worker processes and pods."""
    lock_dir = cache_dir / ".rebuild-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{source_sha256}.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _completed_cache_rebuild(
    cache_dir: Path,
    cache_path: Path,
    message: ReceiptMessage,
) -> bool:
    """Return true only while this request's exact cache artifact remains valid."""
    marker = _cache_rebuild_marker(cache_dir, message)
    if not marker.is_file() or not cache_path.is_file():
        return False
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
        expected = {
            "request_id": message.request_id,
            "source_sha256": message.source_sha256,
            "cache_sha256": backlog.scan.sha256_file(cache_path),
        }
        if message.batch_id:
            expected.update({
                "batch_id": message.batch_id,
                "batch_index": message.batch_index,
                "batch_total": message.batch_total,
            })
        return value == expected
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False


def _mark_cache_rebuild_complete(
    cache_dir: Path,
    cache_path: Path,
    message: ReceiptMessage,
) -> None:
    """Atomically record that a request produced the current cache artifact."""
    if not cache_path.is_file():
        raise RuntimeError("cache rebuild did not produce its expected artifact")
    marker = _cache_rebuild_marker(cache_dir, message)
    marker.parent.mkdir(parents=True, exist_ok=True)
    temporary = marker.with_name(f".{marker.name}.{os.getpid()}.tmp")
    payload = {
        "request_id": message.request_id,
        "source_sha256": message.source_sha256,
        "cache_sha256": backlog.scan.sha256_file(cache_path),
    }
    if message.batch_id:
        payload.update({
            "batch_id": message.batch_id,
            "batch_index": message.batch_index,
            "batch_total": message.batch_total,
        })
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, marker)
    finally:
        temporary.unlink(missing_ok=True)


def _cache_batch_progress(cache_dir: Path, message: ReceiptMessage) -> tuple[int, int] | None:
    if not message.batch_id or message.batch_total is None:
        return None
    markers = cache_dir / ".rebuild-batches" / message.batch_id
    completed = sum(1 for path in markers.glob("*.json") if path.is_file())
    return completed, message.batch_total


def _work_log_context(message: ReceiptMessage | None) -> str:
    if message is None:
        return f"worker_host={socket.gethostname()} worker_pid={os.getpid()}"
    progress = (
        f" batch_id={message.batch_id} progress={message.batch_index}/{message.batch_total}"
        if message.batch_id else ""
    )
    return (
        f"worker_host={socket.gethostname()} worker_pid={os.getpid()} "
        f"source_reference={message.source_reference} request_id={message.request_id}{progress}"
    )


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


def publish_confirmed(
    channel,
    exchange: str,
    body: bytes,
    *,
    message_id: str | None = None,
    error: str | None = None,
    error_message: str | None = None,
    message_type: str = "receipt.process.v1",
    routing_key: str = "receipt",
) -> None:
    import pika

    channel.basic_publish(
        exchange=exchange,
        routing_key=routing_key,
        body=body,
        mandatory=True,
        properties=pika.BasicProperties(
            content_type="application/json",
            content_encoding="utf-8",
            delivery_mode=2,
            message_id=message_id,
            timestamp=int(time.time()),
            type=message_type,
            headers={
                key: value
                for key, value in {
                    "error_type": error,
                    "error_message": error_message,
                }.items()
                if value
            },
        ),
    )


def publication_plan(
    conn, root: Path, ingest_schema: str = "ingest", *, cache_dir: Path | None = None,
) -> tuple[list[ReceiptMessage], dict[str, int]]:
    """Queue unfinished receipts and refresh completed receipts missing OCR caches."""
    if not root.is_dir():
        raise ValueError("receipt root must be an existing directory")
    if cache_dir is None:
        cache_dir = Path(backlog._default_path(
            "receipts/derived/ocr-cache", "/data/receipts/derived/ocr-cache", "HOME_BUDGET_OCR_CACHE",
        )).expanduser().resolve()
    plan = backlog.plan_unprocessed_receipts(conn, root, ingest_schema=ingest_schema)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT source_sha256 FROM {ingest_schema}.receipt_processing_status WHERE attempts >= %s",
            (MAX_ATTEMPTS,),
        )
        exhausted = {row[0] for row in cur.fetchall()}
    missing_cache = [
        candidate for candidate in plan.skipped
        if not has_ocr_cache(cache_dir, candidate.path.relative_to(root).as_posix(), candidate.source_sha256)
    ]
    completed = {}
    if missing_cache:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT source_sha256, EXTRACT(EPOCH FROM completed_at)::text "
                f"FROM {ingest_schema}.receipt_processing_status "
                "WHERE source_sha256 = ANY(%s) AND status IN ('succeeded', 'review_required')",
                ([candidate.source_sha256 for candidate in missing_cache],),
            )
            completed = dict(cur.fetchall())
    conn.commit()
    messages = {}
    for candidate in plan.pending:
        if candidate.source_sha256 in exhausted:
            continue
        reference = candidate.path.relative_to(root).as_posix()
        message = ReceiptMessage(candidate.source_sha256, reference)
        message.resolve_source(root)
        messages.setdefault(message.message_id, message)
    reprocess_messages = {}
    for candidate in missing_cache:
        if candidate.source_sha256 not in completed:
            continue  # Another worker has already started processing this source.
        reference = candidate.path.relative_to(root).as_posix()
        # Repeated scans of the same completion publish the same request. A later
        # completion followed by cache deletion creates a fresh request/budget.
        request_id = str(uuid5(
            NAMESPACE_URL,
            f"ledger:missing-ocr-cache:{candidate.source_sha256}:{reference}:{completed[candidate.source_sha256]}",
        ))
        message = ReceiptMessage(candidate.source_sha256, reference, version=2, request_id=request_id)
        message.resolve_source(root)
        reprocess_messages.setdefault(message.message_id, message)
    messages.update(reprocess_messages)
    return list(messages.values()), {
        "discovered": len(plan.discovered),
        "skipped": len(plan.skipped) - len(reprocess_messages),
        "exhausted": sum(c.source_sha256 in exhausted for c in plan.pending),
        "cache_missing": len(reprocess_messages),
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
    _print_json({
        "source_reference": message.source_reference,
        "source_sha256": message.source_sha256,
        "request_id": request_id,
        "queue": topology.work,
        "status": "queued",
    })
    return 0


def publish_batch(args, *, cache_only: bool = False) -> int:
    """Queue one request per source, with a stable identity for batch retries."""
    from pika.exceptions import AMQPError

    batch_id = args.request_id or str(uuid4())
    published = 0
    discovered = 0
    selected = 0
    skipped_existing = 0
    topology = Topology()
    try:
        if not args.rabbitmq_url:
            raise ValueError("RABBITMQ_URL or --rabbitmq-url is required")
        namespace = UUID(batch_id)
        if str(namespace) != batch_id:
            raise ValueError("request_id must be a canonical UUID")
        root = Path(args.receipt_root).expanduser().resolve()
        if not root.is_dir():
            raise ValueError("receipt root must be an existing directory")
        paths = backlog.scan.discover_scans(root)
        discovered = len(paths)
        source_reference = getattr(args, "source_reference", None)
        if source_reference:
            selector = ReceiptMessage(
                "0" * 64,
                source_reference,
                version=3,
                request_id=str(uuid5(namespace, f"selector:{source_reference}")),
            )
            selected_path = selector.resolve_source(root)
            if not selected_path.is_file():
                raise ValueError(f"Receipt not found: {source_reference}")
            if selected_path.suffix.lower() not in backlog.scan.SUPPORTED_EXTS:
                raise ValueError(f"Unsupported receipt file: {source_reference}")
            paths = [selected_path]

        sources = []
        cache_dir = Path(args.ocr_cache).expanduser().resolve()
        for path in paths:
            reference = path.relative_to(root).as_posix()
            source_sha256 = backlog.scan.sha256_file(path)
            if (
                cache_only
                and getattr(args, "missing_only", False)
                and has_ocr_cache(cache_dir, reference, source_sha256)
            ):
                skipped_existing += 1
                continue
            sources.append((reference, source_sha256))
        selected = len(sources)

        connection = connect_broker(args.rabbitmq_url)
        try:
            channel = connection.channel()
            declare_topology(channel, topology)
            for index, (reference, source_sha256) in enumerate(sources, start=1):
                request = ReceiptMessage(
                    source_sha256, reference, version=3 if cache_only else 2,
                    request_id=str(uuid5(namespace, f"{'cache' if cache_only else 'reprocess'}:{reference}")),
                    batch_id=batch_id if cache_only else None,
                    batch_index=index if cache_only else None,
                    batch_total=selected if cache_only else None,
                )
                request.resolve_source(root)
                publish_confirmed(
                    channel, topology.work, request.to_bytes(),
                    message_id=request.message_id, message_type=request.message_type,
                )
                published += 1
                if args.verbose:
                    print(
                        f"[{published}/{selected}] Queued {reference} batch={batch_id}",
                        file=sys.stderr,
                        flush=True,
                    )
        finally:
            if connection.is_open:
                connection.close()
    except (ValueError, RuntimeError, OSError, AMQPError) as exc:
        detail = str(exc) if type(exc) is ValueError else type(exc).__name__
        print(f"Cannot finish batch {batch_id}: {detail}; {published} publication(s) confirmed.", file=sys.stderr)
        print(f"Retry with --request-id {batch_id} to reuse these requests.", file=sys.stderr)
        return 1
    summary = {
        "discovered": discovered,
        "selected": selected,
        "published": published,
        "request_id": batch_id,
        "queue": topology.work,
        "status": "queued",
    }
    if cache_only:
        summary["skipped_existing"] = skipped_existing
    _print_json(summary)
    return 0 if selected else 2


def rebuild_cache_message(conn, message: ReceiptMessage, root: Path, cache_dir: Path, budget_schema: str) -> str:
    """Rebuild on the consumer, preserving extraction and ingest completion state."""
    backlog.validate_schema(budget_schema)
    path = message.resolve_source(root)
    table = f"{budget_schema}.receipt_cache_requests"
    key = (message.source_sha256, message.request_id)
    with backlog.receipt_lock(conn, message.source_sha256):
        with conn.cursor() as cur:
            cur.execute(f"INSERT INTO {table} (source_sha256, request_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", key)
            cur.execute(f"SELECT attempts, completed_at FROM {table} WHERE source_sha256 = %s AND request_id = %s FOR UPDATE", key)
            attempts, completed_at = cur.fetchone()
            if completed_at is not None:
                conn.commit()
                return "skipped"
            if attempts >= MAX_ATTEMPTS:
                raise backlog.RetryExhausted("cache rebuild request attempt limit reached")
            cur.execute(f"UPDATE {table} SET attempts = attempts + 1 WHERE source_sha256 = %s AND request_id = %s", key)
        conn.commit()
        if backlog.scan.sha256_file(path) != message.source_sha256:
            raise InvalidReceiptMessage("source contents changed since publication")
        LOG.info("receipt=%s Rebuilding OCR cache %s", message.message_id, message.source_reference)
        receipts = backlog.parse_scans_parallel([path], root, 1, cache_dir, True)
        if len(receipts) != 1 or receipts[0].source_sha256 != message.source_sha256 or backlog.scan.sha256_file(path) != message.source_sha256:
            raise InvalidReceiptMessage("source contents changed during cache rebuild")
        with conn.cursor() as cur:
            cur.execute(f"UPDATE {table} SET completed_at = NOW() WHERE source_sha256 = %s AND request_id = %s", key)
        conn.commit()
        return "cache_rebuilt"


def process_message(message: ReceiptMessage, args) -> str:
    root = Path(args.receipt_root).expanduser().resolve()
    message.resolve_source(root)
    path = root / message.source_reference
    conn = backlog.scan._db_connect(args.db_dsn)
    try:
        if message.version == 3:
            with telemetry.receipt_process(
                message.source_reference, message.source_sha256,
            ) as receipt_trace:
                status = rebuild_cache_message(
                    conn, message, root, Path(args.ocr_cache).expanduser().resolve(), args.budget_schema,
                )
                receipt_trace.finish(status)
                return status
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


def produce_result_messages(message: ReceiptMessage, args):
    """Run OCR without database access and return durable result events."""
    from .ocr_results import result_messages_for_receipt

    root = Path(args.receipt_root).expanduser().resolve()
    path = message.resolve_source(root)
    cache_dir = Path(args.ocr_cache).expanduser().resolve()
    cache_path = _cache_path(
        cache_dir, message.source_reference, message.source_sha256,
    )
    if not path.is_file():
        raise InvalidReceiptMessage("receipt source does not exist")
    if backlog.scan.sha256_file(path) != message.source_sha256:
        raise InvalidReceiptMessage("source contents changed since publication")
    lock = cache_rebuild_lock(cache_dir, message.source_sha256)
    with lock:
        cache_reused = message.version in (2, 3) and _completed_cache_rebuild(
            cache_dir, cache_path, message,
        )
        force_refresh = message.version in (2, 3) and not cache_reused
        with telemetry.receipt_process(
            message.source_reference, message.source_sha256,
        ) as receipt_trace:
            parsed = backlog.parse_scans_parallel(
                [path],
                root,
                1,
                cache_dir,
                force_refresh,
                return_cache_hits=True,
                progress=lambda text: LOG.info(
                    "receipt=%s %s %s", message.message_id, _work_log_context(message), text,
                ),
            )
            if isinstance(parsed, tuple):
                receipts, cache_hits = parsed
            else:
                receipts, cache_hits = parsed, 0
            telemetry.record_cache_lookup(cache_hits > 0)
            if len(receipts) != 1:
                raise RuntimeError(f"expected one parsed receipt, got {len(receipts)}")
            receipt = receipts[0]
            run_uuid = (getattr(receipt, "ocr_run", None) or {}).get("run_uuid")
            telemetry.adopt_run_uuid(run_uuid)
            if (
                receipt.source_sha256 != message.source_sha256
                or backlog.scan.sha256_file(path) != message.source_sha256
            ):
                raise InvalidReceiptMessage("source contents changed during processing")
            if message.version in (2, 3) and not cache_reused:
                _mark_cache_rebuild_complete(cache_dir, cache_path, message)
            batch_progress = _cache_batch_progress(cache_dir, message)
            if batch_progress:
                LOG.info(
                    "receipt=%s %s status=cache_ready completed=%s/%s",
                    message.message_id,
                    _work_log_context(message),
                    batch_progress[0],
                    batch_progress[1],
                )
            mode = "cache_only" if message.version == 3 else (
                "reprocess" if message.version == 2 else "normal"
            )
            events = result_messages_for_receipt(
                receipt,
                cache_path=cache_path,
                cache_root=cache_dir,
                persistence_mode=mode,
                request_id=message.request_id,
            )
            # This worker completed computation and confirmed result publication;
            # only the collector may report durable success after its DB commit.
            status = (
                "cache_results_republished" if cache_reused else "cache_results_published"
            ) if mode == "cache_only" else (
                "review_results_published"
                if receipt.extraction_status != "complete"
                else "results_published"
            )
            receipt_trace.finish(status)
            return status, events


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


def handle_worker_delivery(
    connection,
    channel,
    executor,
    delivery_tag: int,
    body: bytes,
    process,
    topology: Topology,
    result_topology,
) -> None:
    """Publish every result with confirms before acknowledging OCR work."""
    from .ocr_collector import publish_result

    message = None
    try:
        message = ReceiptMessage.from_bytes(body)
        LOG.info(
            "receipt=%s %s status=active attempt=%s",
            message.message_id,
            _work_log_context(message),
            message.attempt,
        )
        status, events = process_with_heartbeats(
            connection, executor, process, message,
        )
        for event in events:
            publish_result(channel, result_topology.work, event)
    except Exception as exc:
        permanent = isinstance(exc, InvalidReceiptMessage)
        retry = message is not None and not permanent and message.attempt < MAX_ATTEMPTS
        target = topology.retry if retry else topology.dead
        next_body = replace(message, attempt=message.attempt + 1).to_bytes() if retry else body
        publish_confirmed(
            channel,
            target,
            next_body,
            message_id=message.message_id if message else None,
            error=type(exc).__name__,
            error_message=str(exc)[:512],
            message_type=message.message_type if message else "receipt.process.v1",
        )
        LOG.warning(
            "receipt=%s %s status=%s routed=%s error=%s attempt=%s",
            message.message_id if message else "invalid",
            _work_log_context(message),
            "retried" if retry else "failed",
            target,
            type(exc).__name__,
            message.attempt if message else 0,
        )
    else:
        LOG.info(
            "receipt=%s %s status=%s result_events=%s",
            message.message_id,
            _work_log_context(message),
            status,
            len(events),
        )
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


def consume_worker(
    connection,
    channel,
    process,
    topology: Topology,
    result_topology,
    stop: Event,
) -> None:
    """Consume source work while keeping AMQP operations on their owner thread."""
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
                handle_worker_delivery(
                    connection,
                    channel,
                    executor,
                    method.delivery_tag,
                    body,
                    process,
                    topology,
                    result_topology,
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
    telemetry.configure_logging()
    telemetry.configure_telemetry()
    # Keep the CLI focused on receipt outcomes. Pika's connection workflow logs
    # every socket and AMQP shutdown transition at INFO, which obscures them.
    logging.getLogger("pika").setLevel(logging.WARNING)
    backlog.validate_schema(args.ingest_schema)
    backlog.validate_schema(args.budget_schema)
    missing = []
    if args.queue_mode == "publish" and not args.db_dsn:
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
            messages, summary = publication_plan(
                conn, Path(args.receipt_root).expanduser().resolve(), args.ingest_schema,
                cache_dir=Path(args.ocr_cache).expanduser().resolve(),
            )
        finally:
            conn.close()
    connection = connect_broker(args.rabbitmq_url)
    try:
        channel = connection.channel()
        declare_topology(channel, topology)
        if args.queue_mode == "publish":
            for index, message in enumerate(messages, start=1):
                publish_confirmed(
                    channel, topology.work, message.to_bytes(),
                    message_id=message.message_id, message_type=message.message_type,
                )
                if getattr(args, "verbose", False):
                    print(f"[{index}/{len(messages)}] Queued {message.source_reference}", file=sys.stderr, flush=True)
            _print_json(summary)
        else:
            from .ocr_collector import ResultTopology, declare_result_topology

            result_topology = ResultTopology()
            declare_result_topology(channel, result_topology)
            stop = Event()
            previous = {sig: signal.signal(sig, lambda signum, frame: stop.set()) for sig in (signal.SIGINT, signal.SIGTERM)}
            try:
                consume_worker(
                    connection,
                    channel,
                    lambda message: produce_result_messages(message, args),
                    topology,
                    result_topology,
                    stop,
                )
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
    finally:
        if connection.is_open:
            connection.close()
        telemetry.shutdown_telemetry()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(required=True)
    for mode in ("publish", "consume"):
        add_arguments(commands.add_parser(mode), mode)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
