"""Recover only the known cache-only missing-parent failures, with private evidence."""
import hashlib
import json
import os
from pathlib import Path

from .ocr_results import OcrResultMessage, RUN_COMPLETED, PASS_COMPLETED, InvalidOcrResultMessage

MISSING_SOURCE = 'cache-only result has no existing receipt evidence'
MISSING_RUN = 'pass result arrived before its durable OCR run'


def eligible_deliveries(deliveries):
    """Run events must be restored before their passes; unrelated failures stay put."""
    parsed = []
    runs = set()
    for tag, headers, body in deliveries:
        try:
            event = OcrResultMessage.from_bytes(body)
        except (InvalidOcrResultMessage, ValueError, TypeError):
            continue
        parsed.append((tag, headers, event))
        if (event.event_type == RUN_COMPLETED and event.payload['persistence_mode'] == 'cache_only'
                and headers.get('error_message') == MISSING_SOURCE):
            runs.add(event.run_uuid)
    selected = []
    for tag, headers, event in parsed:
        if event.run_uuid not in runs:
            continue
        if ((event.event_type == RUN_COMPLETED and headers.get('error_message') == MISSING_SOURCE)
                or (event.event_type == PASS_COMPLETED and headers.get('error_message') == MISSING_RUN)):
            selected.append((tag, event))
    return sorted(selected, key=lambda item: (item[1].event_type != RUN_COMPLETED, item[1].run_uuid,
                                              item[1].pass_id or 0))


def recover_deliveries(channel, deliveries, process, *, apply):
    selected = eligible_deliveries(deliveries)
    acked = set()
    errors = {}
    if apply:
        for tag, event in selected:
            try:
                process(event)  # persist_result commits before returning.
            except Exception as exc:
                errors[type(exc).__name__] = errors.get(type(exc).__name__, 0) + 1
            else:
                channel.basic_ack(delivery_tag=tag)
                acked.add(tag)
    for tag, _, _ in deliveries:
        if tag not in acked:
            channel.basic_nack(delivery_tag=tag, requeue=True)
    return {'inspected': len(deliveries), 'eligible': len(selected), 'recovered': len(acked),
            'retained': len(deliveries) - len(acked), 'errors': errors, 'applied': apply}


def run_recovery(args):
    import psycopg
    from ..development_backend import validate_endpoints, verify_database
    from .ocr_collector import persist_result, ResultTopology
    from .queue_ingest import connect_broker
    if not 1 <= args.limit <= 10000:
        raise ValueError('Choose --limit between 1 and 10000')
    validate_endpoints(os.environ)
    with psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=5) as conn:
        verify_database(conn)
        conn.rollback()
    directory = Path(args.evidence_dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    directory.chmod(0o700)
    connection = connect_broker(os.environ['RABBITMQ_URL'])
    try:
        channel = connection.channel()
        dead = ResultTopology().dead
        count = channel.queue_declare(queue=dead, passive=True).method.message_count
        deliveries = []
        manifest = []
        for index in range(min(count, args.limit)):
            method, properties, body = channel.basic_get(queue=dead, auto_ack=False)
            if method is None:
                break
            target = directory / f'{index:05d}.json'
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as output:
                output.write(body)
                output.flush()
                os.fsync(output.fileno())
            headers = properties.headers or {}
            deliveries.append((method.delivery_tag, headers, body))
            manifest.append({'file': target.name, 'message_id': properties.message_id,
                             'headers': headers, 'sha256': hashlib.sha256(body).hexdigest()})
        record = directory / 'manifest.private.json'
        record.write_text(json.dumps(manifest, indent=2, default=str))
        record.chmod(0o600)
        def process(event):
            with psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=5) as conn:
                verify_database(conn)
                return persist_result(conn, event)
        report = recover_deliveries(channel, deliveries, process, apply=args.apply)
        report['remaining_dead_observed'] = channel.queue_declare(queue=dead, passive=True).method.message_count
        report['evidence_dir'] = str(directory)
        record = directory / 'result.private.json'
        record.write_text(json.dumps(report, indent=2))
        record.chmod(0o600)
        print(json.dumps(report, sort_keys=True))
        return 1 if report['errors'] else 0
    finally:
        # Any delivery not acknowledged after a successful commit is requeued.
        connection.close()
