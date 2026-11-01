"""Both configured sources retain one queue/cache/evidence namespace."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from home_budget_pipeline import cli
from home_budget_pipeline.receipts import queue_ingest as queue, parallel_ingest
from home_budget_pipeline.receipts.message import ReceiptMessage, InvalidReceiptMessage
from home_budget_pipeline.receipts.sources import discover_sources, reference_for


@pytest.fixture
def roots(tmp_path, monkeypatch):
    scanned = tmp_path / 'scanned'
    electronic = tmp_path / 'electronic'
    for root in (scanned, electronic):
        (root / '2028-03-04').mkdir(parents=True)
    scan = scanned / '2028-03-04' / 'scan.pdf'
    pdf = electronic / '2028-03-04' / 'download.pdf'
    scan.write_bytes(b'scan')
    pdf.write_bytes(b'download')
    monkeypatch.setenv('RECEIPT_SOURCE_ROOT', str(scanned))
    monkeypatch.setenv('ELECTRONIC_RECEIPT_SOURCE_ROOT', str(electronic))
    return scanned, electronic, scan, pdf


def test_discovery_and_worker_resolution_share_both_roots(roots):
    scanned, electronic, scan, pdf = roots
    assert set(discover_sources(scanned)) == {scan, pdf}
    for path, ref in [(scan, '2028-03-04/scan.pdf'), (pdf, 'electronic/2028-03-04/download.pdf')]:
        assert reference_for(path, scanned) == ref
        assert ReceiptMessage('a'*64, ref).resolve_source(scanned) == path


def test_scoped_input_does_not_pick_up_configured_electronic_root(roots):
    scanned, _, scan, _ = roots
    assert discover_sources(scan.parent) == [scan]


def test_missing_electronic_mount_fails_before_publication(roots):
    scanned, electronic, _, pdf = roots
    pdf.unlink()
    pdf.parent.rmdir()
    electronic.rmdir()
    with pytest.raises(ValueError, match='does not exist'):
        queue.publication_plan(MagicMock(), scanned)


def test_electronic_symlink_cannot_escape_its_root(roots, tmp_path):
    scanned, electronic, _, _ = roots
    outside = tmp_path / 'outside.pdf'
    outside.write_bytes(b'outside')
    (electronic / 'escape.pdf').symlink_to(outside)
    with pytest.raises(InvalidReceiptMessage, match='outside'):
        ReceiptMessage('a'*64, 'electronic/escape.pdf').resolve_source(scanned)
    with pytest.raises(ValueError, match='outside'):
        discover_sources(scanned)


def test_overlapping_configured_roots_are_rejected(roots, monkeypatch):
    scanned, _, _, _ = roots
    monkeypatch.setenv('ELECTRONIC_RECEIPT_SOURCE_ROOT', str(scanned / 'electronic'))
    with pytest.raises(ValueError, match='separate directories'):
        discover_sources(scanned)


@pytest.mark.parametrize('same_bytes', [False, True])
def test_backlog_publication_covers_both_and_deduplicates_hashes(roots, monkeypatch, same_bytes):
    scanned, _, scan, pdf = roots
    if same_bytes:
        pdf.write_bytes(scan.read_bytes())
    def plan(conn, root, **kwargs):
        paths = queue.backlog.scan.discover_scans(root)
        return SimpleNamespace(discovered=paths, skipped=[], pending=[
            SimpleNamespace(path=p, source_sha256=queue.backlog.scan.sha256_file(p)) for p in paths])
    monkeypatch.setattr(queue.backlog, 'plan_unprocessed_receipts', plan)
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchall.return_value = []
    messages, summary = queue.publication_plan(conn, scanned)
    assert summary['discovered'] == 2
    assert len(messages) == summary['published'] == (1 if same_bytes else 2)
    if not same_bytes:
        assert {m.source_reference for m in messages} == {'2028-03-04/scan.pdf', 'electronic/2028-03-04/download.pdf'}


@pytest.mark.parametrize('command', [['ocr-cache', 'rebuild'], ['receipts', 'process', '--refresh-ocr-cache']])
def test_refresh_commands_publish_both_sources(roots, monkeypatch, command):
    monkeypatch.setenv('RABBITMQ_URL', 'amqp://localhost/')
    broker = MagicMock()
    monkeypatch.setattr(queue, 'connect_broker', lambda _: broker)
    assert cli.main(command) == 0
    messages = [ReceiptMessage.from_bytes(c.kwargs['body'])
                for c in broker.channel.return_value.basic_publish.call_args_list]
    assert {m.source_reference for m in messages} == {'2028-03-04/scan.pdf', 'electronic/2028-03-04/download.pdf'}


def test_cached_parser_preserves_electronic_reference(roots, monkeypatch, tmp_path):
    scanned, _, _, pdf = roots
    monkeypatch.setattr(parallel_ingest, '_read_or_create_pages', lambda *args: (['EXAMPLE SHOP\n2028-03-04\nTOTAL 10.00'], True))
    receipt, hit = parallel_ingest._parse_scan_cached(pdf, scanned, tmp_path / 'cache', False)
    assert hit
    assert receipt.source_reference == 'electronic/2028-03-04/download.pdf'


def test_reserved_electronic_namespace_cannot_hide_scanned_file(roots):
    scanned, _, _, _ = roots
    directory = scanned / 'electronic'
    directory.mkdir()
    (directory / 'other.pdf').write_bytes(b'wrong source')
    with pytest.raises(ValueError, match='reserved'):
        discover_sources(scanned)


def test_reprocess_resolves_electronic_source_directly(roots, monkeypatch):
    monkeypatch.setenv('RABBITMQ_URL', 'amqp://localhost/')
    broker = MagicMock()
    monkeypatch.setattr(queue, 'connect_broker', lambda _: broker)
    monkeypatch.setattr(queue.receipt_lineage, 'start_batch', lambda *a: None)
    assert cli.main(['receipts', 'reprocess', 'electronic/2028-03-04/download.pdf']) == 0
    message = ReceiptMessage.from_bytes(broker.channel.return_value.basic_publish.call_args.kwargs['body'])
    assert message.source_sha256 == queue.backlog.scan.sha256_file(roots[3])
