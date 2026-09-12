import json
from concurrent.futures import Future
from unittest.mock import MagicMock

import pytest

from home_budget_pipeline import cli
from home_budget_pipeline.receipts import parallel_ingest as parallel


def test_verbose_rebuild_reports_progress_during_ocr_without_database(tmp_path, monkeypatch, capsys):
    source = tmp_path / "2026-08-14" / "receipt.pdf"
    source.parent.mkdir()
    source.write_bytes(b"test receipt")
    monkeypatch.setenv("RECEIPT_SOURCE_ROOT", str(tmp_path))
    monkeypatch.setenv("HOME_BUDGET_OCR_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(cli.scan, "_db_connect", lambda *a: pytest.fail("cache rebuild opened database"))

    def parse(path, root, cache, refresh):
        assert path == source
        assert refresh is True
        during = capsys.readouterr()
        assert "[0/1] Starting 2026-08-14/receipt.pdf" in during.err
        assert "Completed" not in during.err
        assert not during.out
        return object(), False

    monkeypatch.setattr(parallel, "_parse_scan_cached", parse)
    assert cli.main(["ocr-cache", "rebuild", "--workers", "1", "--verbose"]) == 0
    output = capsys.readouterr()
    assert "[1/1] Completed 2026-08-14/receipt.pdf" in output.err
    assert json.loads(output.out)["cache_rebuilt"] == 1


def test_failed_rebuild_reports_filename_without_success_summary(tmp_path, monkeypatch, capsys):
    (tmp_path / "bad.pdf").write_bytes(b"test")
    monkeypatch.setattr(parallel, "_parse_scan_cached", MagicMock(side_effect=RuntimeError("OCR failed")))
    with pytest.raises(RuntimeError, match="OCR failed"):
        cli.main(["ocr-cache", "rebuild", str(tmp_path), "--workers", "1", "--verbose"])
    output = capsys.readouterr()
    assert "[0/1] Failed bad.pdf" in output.err
    assert "Completed" not in output.err
    assert not output.out


def test_parallel_progress_uses_completion_order_but_results_keep_input_order(tmp_path, monkeypatch):
    paths = [tmp_path / "slow.pdf", tmp_path / "fast.pdf"]
    futures = []
    class Executor:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def submit(self, fn, args):
            future = Future()
            future.set_result((args[0], False))
            futures.append(future)
            return future
    monkeypatch.setattr(parallel, "ProcessPoolExecutor", Executor)
    monkeypatch.setattr(parallel, "as_completed", lambda pending: reversed(futures))
    messages = []
    result = parallel.parse_scans_parallel(paths, tmp_path, 2, tmp_path / "cache", True, progress=messages.append)
    assert result == list(map(str, paths))
    assert messages == [
        "[0/2] Queued slow.pdf", "[0/2] Queued fast.pdf",
        "[1/2] Completed fast.pdf", "[2/2] Completed slow.pdf",
    ]


def test_empty_verbose_rebuild_returns_empty_summary(tmp_path, capsys):
    assert cli.main(["ocr-cache", "rebuild", str(tmp_path), "--verbose", "--workers", "1"]) == 2
    output = capsys.readouterr()
    assert "0 receipt(s)" in output.err
    assert json.loads(output.out)["cache_rebuilt"] == 0
