from unittest.mock import MagicMock

import pytest

from home_budget_pipeline.receipts import queue_ingest as queue
from home_budget_pipeline.receipts.parallel_ingest import has_ocr_cache


@pytest.fixture
def completed_receipt(tmp_path, monkeypatch):
    root = tmp_path / "inbox"
    path = root / "2026-08-14" / "receipt.pdf"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"receipt")
    cache = tmp_path / "cache"
    candidate = queue.backlog.ReceiptCandidate(path, "a" * 64)
    plan = queue.backlog.DiscoveryPlan((candidate,), (), (candidate,))
    monkeypatch.setattr(queue.backlog, "plan_unprocessed_receipts", lambda *a, **kw: plan)
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    return root, cache, candidate, conn, cursor


def test_missing_cache_queues_completed_receipt_despite_prior_attempt_limit(completed_receipt):
    root, cache, candidate, conn, cursor = completed_receipt
    cursor.fetchall.side_effect = [[(candidate.source_sha256,)], [(candidate.source_sha256, "123.456")]]
    messages, summary = queue.publication_plan(conn, root, cache_dir=cache)
    assert len(messages) == 1
    assert messages[0].version == 2
    assert messages[0].request_id
    assert messages[0].source_reference == "2026-08-14/receipt.pdf"
    assert messages[0].source_sha256 == candidate.source_sha256
    assert summary == {"discovered": 1, "skipped": 0, "exhausted": 0, "cache_missing": 1, "published": 1}
    assert all(call.args[0].lstrip().startswith("SELECT") for call in cursor.execute.call_args_list)


def test_same_completion_reuses_request_but_later_completion_gets_new_request(completed_receipt):
    root, cache, candidate, conn, cursor = completed_receipt
    messages = []
    for epoch in ("123.456", "123.456", "124.789"):
        cursor.fetchall.side_effect = [[], [(candidate.source_sha256, epoch)]]
        messages.append(queue.publication_plan(conn, root, cache_dir=cache)[0][0])
    assert messages[0] == messages[1]
    assert messages[2].request_id != messages[0].request_id


@pytest.mark.parametrize("legacy", [False, True])
def test_existing_current_or_legacy_cache_keeps_completed_receipt_skipped(completed_receipt, legacy):
    root, cache, candidate, conn, cursor = completed_receipt
    cache_file = cache / (f"{candidate.source_sha256}.json" if legacy else "2026-08-14/receipt.pdf.json")
    cache_file.parent.mkdir(parents=True)
    cache_file.write_text("cached")
    cursor.fetchall.return_value = []
    messages, summary = queue.publication_plan(conn, root, cache_dir=cache)
    assert messages == []
    assert summary["skipped"] == 1
    assert summary["cache_missing"] == 0


def test_source_that_started_processing_during_discovery_is_not_requeued(completed_receipt):
    root, cache, _, conn, cursor = completed_receipt
    cursor.fetchall.side_effect = [[], []]
    assert queue.publication_plan(conn, root, cache_dir=cache)[0] == []


def test_cache_directory_is_not_treated_as_cache_file(completed_receipt):
    _, cache, candidate, _, _ = completed_receipt
    (cache / "2026-08-14/receipt.pdf.json").mkdir(parents=True)
    assert not has_ocr_cache(cache, "2026-08-14/receipt.pdf", candidate.source_sha256)


def test_publisher_passes_configured_cache_and_reprocess_message_type(completed_receipt, monkeypatch, capsys):
    from types import SimpleNamespace

    root, cache, candidate, conn, cursor = completed_receipt
    cursor.fetchall.side_effect = [[], [(candidate.source_sha256, "123.456")]]
    monkeypatch.setattr(queue.backlog, "process_candidate", lambda *a, **kw: pytest.fail("publisher bypassed RabbitMQ"))
    monkeypatch.setattr(queue.backlog, "parse_scans_parallel", lambda *a, **kw: pytest.fail("publisher ran OCR"))
    monkeypatch.setattr(queue.backlog.scan, "_db_connect", lambda dsn: conn)
    broker = MagicMock()
    monkeypatch.setattr(queue, "connect_broker", lambda url: broker)
    args = SimpleNamespace(
        queue_mode="publish", db_dsn="dbname=test", rabbitmq_url="amqp://localhost/",
        ingest_schema="ingest", budget_schema="budget", receipt_root=str(root), ocr_cache=str(cache),
    )
    assert queue.run(args) == 0
    publication = broker.channel.return_value.basic_publish.call_args.kwargs
    assert publication["properties"].type == "receipt.reprocess.v2"
    assert '"cache_missing": 1' in capsys.readouterr().out
    conn.close.assert_called_once()
