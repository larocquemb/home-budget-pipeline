import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from home_budget_pipeline import cli
from home_budget_pipeline.receipts import backlog_ingest as backlog


REFERENCE = "2026-08-14/receipts_20260814_0001.pdf"


@pytest.fixture
def receipt_env(tmp_path, monkeypatch):
    root = tmp_path / "inbox"
    path = root / REFERENCE
    path.parent.mkdir(parents=True)
    path.write_bytes(b"selected receipt")
    (path.parent / "other.pdf").write_bytes(b"unrelated receipt")
    cache = tmp_path / "cache"
    monkeypatch.setenv("RECEIPT_SOURCE_ROOT", str(root))
    monkeypatch.setenv("HOME_BUDGET_OCR_CACHE", str(cache))
    monkeypatch.setenv("DATABASE_URL", "dbname=test")
    conn = MagicMock()
    connect = MagicMock(return_value=conn)
    monkeypatch.setattr(cli.scan, "_db_connect", connect)
    return root, path, cache, conn, connect


@pytest.mark.parametrize("extraction_status,expected", [("complete", "succeeded"), ("partial", "review_required")])
def test_reprocess_refreshes_only_selected_completed_receipt(receipt_env, monkeypatch, capsys, extraction_status, expected):
    root, path, cache, conn, connect = receipt_env
    sha = cli.scan.sha256_file(path)
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [(sha,)]  # Already completed, if queried.

    def parse(paths, receipt_root, workers, cache_dir, refresh):
        assert paths == [path]
        assert receipt_root == root
        assert cache_dir == cache
        assert workers == 1
        assert refresh is True
        return [SimpleNamespace(
            source_reference=paths[0].relative_to(receipt_root).as_posix(),
            source_sha256=sha, extraction_status=extraction_status,
        )]

    monkeypatch.setattr(backlog, "parse_scans_parallel", parse)
    persist = MagicMock()
    monkeypatch.setattr(backlog, "persist_evidence_first", persist)
    assert cli.main(["receipts", "reprocess", REFERENCE, "--verbose"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == {"source_reference": REFERENCE, "status": expected}
    assert f"Running OCR and parsing {REFERENCE}" in output.err
    persist.assert_called_once()
    assert persist.call_args.args[1][0].source_reference == REFERENCE
    assert persist.call_args.kwargs == {"replace_existing": True}
    queries = [call.args[0] for call in cursor.execute.call_args_list]
    assert any("pg_advisory_lock" in sql for sql in queries)
    assert any("pg_advisory_unlock" in sql for sql in queries)
    connect.assert_called_once_with("dbname=test")
    conn.close.assert_called_once()


@pytest.mark.parametrize("reference", ["../outside.pdf", "/tmp/outside.pdf", "./receipt.pdf", "a//b.pdf", "a\\b.pdf", "missing.pdf", "2026-08-14"])
def test_reprocess_rejects_invalid_sources_before_database_access(receipt_env, capsys, reference):
    *_, connect = receipt_env
    assert cli.main(["receipts", "reprocess", reference]) == 1
    assert "Cannot reprocess receipt:" in capsys.readouterr().err
    connect.assert_not_called()


def test_reprocess_rejects_symlink_outside_inbox(receipt_env, tmp_path):
    root, _, _, _, connect = receipt_env
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"outside")
    (root / "link.pdf").symlink_to(outside)
    assert cli.main(["receipts", "reprocess", "link.pdf"]) == 1
    connect.assert_not_called()


def test_reprocess_requires_database_configuration(receipt_env, monkeypatch, capsys):
    *_, connect = receipt_env
    monkeypatch.delenv("DATABASE_URL")
    monkeypatch.delenv("HOME_BUDGET_PG_DSN", raising=False)
    assert cli.main(["receipts", "reprocess", REFERENCE]) == 1
    assert "Missing database DSN" in capsys.readouterr().err
    connect.assert_not_called()


def test_reprocess_records_ocr_failure_and_closes_connection(receipt_env, monkeypatch, capsys):
    _, _, _, conn, _ = receipt_env
    monkeypatch.setattr(backlog, "parse_scans_parallel", MagicMock(side_effect=RuntimeError("OCR failed")))
    persist = MagicMock()
    monkeypatch.setattr(backlog, "persist_evidence_first", persist)
    assert cli.main(["receipts", "reprocess", REFERENCE]) == 1
    output = capsys.readouterr()
    assert "Cannot reprocess receipt: OCR failed" in output.err
    assert not output.out
    persist.assert_not_called()
    cursor = conn.cursor.return_value.__enter__.return_value
    assert any("'failed'" in call.args[0] for call in cursor.execute.call_args_list)
    conn.close.assert_called_once()
