"""Bulk cached extraction over active scanned and electronic sources."""
from pathlib import Path, PurePosixPath
import sys
from uuid import UUID, uuid4, uuid5

from . import queue_ingest as queue
from .message import ReceiptMessage
from .ocr_collector import ResultTopology, declare_result_topology, publish_result
from .sources import discover_sources, reference_for


def run(args):
    from pika.exceptions import AMQPError

    connection = None
    message = None
    batch_started = False
    summary = {
        'request_id': args.request_id or str(uuid4()),
        'mode': 'apply' if args.apply else 'preview',
        'selected': 0, 'eligible': 0, 'published': 0, 'missing_cache': 0,
        'failed': [], 'receipts': [], 'ocr_executed': False,
    }
    try:
        namespace = UUID(summary['request_id'])
        if str(namespace) != summary['request_id']:
            raise ValueError('request_id must be a canonical UUID')
        prefix = args.source_prefix
        if prefix:
            if (PurePosixPath(prefix).is_absolute() or '\\' in prefix
                    or any(part in {'', '.', '..'} for part in prefix.rstrip('/').split('/'))):
                raise ValueError('source-prefix must be a relative source folder')
            prefix = prefix.rstrip('/') + '/'
        if args.apply and not args.rabbitmq_url:
            raise ValueError('RABBITMQ_URL or --rabbitmq-url is required')
        root = Path(args.receipt_root).expanduser().resolve()
        cache = Path(args.ocr_cache).expanduser().resolve()
        topology = ResultTopology()
        candidates = []
        # Discover active inputs, so deleted sources and orphan caches never publish.
        for path in sorted(discover_sources(root)):
            reference = reference_for(path, root)
            if not reference.startswith(prefix):
                continue
            summary['selected'] += 1
            if not queue._cache_path(cache, reference, '0' * 64).is_file():
                summary['missing_cache'] += 1
                continue
            candidates.append((path, reference))
        for path, reference in candidates:
            message = None
            try:
                digest = queue.backlog.scan.sha256_file(path)
                message = ReceiptMessage(digest, reference, version=2,
                    request_id=str(uuid5(namespace, f'{reference}:{digest}')))
                if args.apply:
                    if not batch_started:
                        queue.receipt_lineage.start_batch(
                            message, summary['request_id'], len(candidates), topology.work,
                        )
                        batch_started = True
                    queue.receipt_lineage.record(
                        message, 'active', batch_id=summary['request_id'], queue=topology.work,
                    )
                with queue.cached_reprocess_events(message, args) as (receipt, events):
                    summary['eligible'] += 1
                    if args.apply:
                        if connection is None:
                            connection = queue.connect_broker(args.rabbitmq_url)
                            channel = connection.channel()
                            declare_result_topology(channel, topology)
                        for event in events:
                            publish_result(channel, topology.work, event)
                        summary['published'] += 1
                        queue.receipt_lineage.record(
                            message, 'results_published', batch_id=summary['request_id'],
                            queue=topology.work, cache_hit=True,
                            result_runs=tuple(dict.fromkeys(event.run_uuid for event in events)),
                        )
                    summary['receipts'].append({
                        'source_reference': reference, 'request_id': message.request_id,
                        'extraction_status': receipt.extraction_status,
                    })
                    if args.verbose:
                        print(f"{summary['mode']}: {reference}", file=sys.stderr, flush=True)
            except (ValueError, OSError) as exc:
                if args.apply and message is not None:
                    queue.receipt_lineage.record(
                        message, 'failed', batch_id=summary['request_id'],
                        queue=topology.work, error_type=type(exc).__name__,
                    )
                summary['failed'].append({'source_reference': reference,
                    'error': str(exc) if isinstance(exc, ValueError) else type(exc).__name__})
    except (ValueError, RuntimeError, OSError, AMQPError) as exc:
        if args.apply and message is not None and batch_started:
            # A lost publish confirmation does not prove success or failure.
            queue.receipt_lineage.record(
                message, 'unknown', batch_id=summary['request_id'],
                queue=topology.work, error_type=type(exc).__name__,
            )
        summary['error'] = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
    finally:
        if connection is not None and connection.is_open:
            connection.close()
    queue._print_json(summary)
    return 1 if summary['failed'] or 'error' in summary else 0
