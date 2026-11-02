"""Synthetic images exercise format parity without running real OCR or models."""
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

from home_budget_pipeline.receipts import ingest


@pytest.mark.parametrize("extension", ["tif", "tiff", "png", "jpg", "jpeg"])
def test_images_run_shared_candidates_and_retain_traced_observations(tmp_path, monkeypatch, extension):
    path = tmp_path / f"receipt.{extension}"
    Image.new("RGB", (120, 180), "white").save(path, dpi=(300, 300))
    original = path.read_bytes()
    monkeypatch.setattr(ingest, "_paddle_ocr_enabled", lambda: True)
    calls = []

    def tesseract(file, *, dpi, psm):
        with Image.open(file) as image:
            calls.append((dpi, psm, image.size))
        return ingest.OCRCandidate("TOTAL 24.60", (ingest.OCRLine("TOTAL 24.60", 90),), dpi, psm)

    monkeypatch.setattr(ingest, "_run_tesseract_candidate", tesseract)
    monkeypatch.setattr(ingest, "_run_paddle_candidate", lambda image, *, dpi: ingest.OCRCandidate(
        "TOTAL 24.60", (ingest.OCRLine("TOTAL 24.60", 98),), dpi, "paddle", "paddle"))
    metrics = []
    with patch.object(ingest.telemetry, "start_ocr_pass") as start, \
         patch.object(ingest.telemetry, "finish_ocr_pass") as finish:
        pages = ingest.extract_page_text(path, metrics, run_uuid="test-image-run")
    assert pages == ["TOTAL 24.60"]
    assert len(calls) == len(ingest.OCR_RENDER_DPIS) * 2 * len(ingest.OCR_PAGE_SEGMENTATION_MODES) + 1
    assert any(m["engine"] == "paddle" for m in metrics)
    assert len(metrics) == len(calls) + 1 == start.call_count == finish.call_count
    assert {m["variant"] for m in metrics} == {"raw", "enhanced", "high_detail"}
    assert [m["pass_id"] for m in metrics] == list(range(1, len(metrics) + 1))
    assert all(m["run_uuid"] == "test-image-run" and m["page_number"] == 1 for m in metrics)
    assert sum(m["selected_base"] for m in metrics) == 1
    assert all(m["provenance"]["source_image"]["dpi_basis"] == "image_metadata" for m in metrics)
    assert calls[0][2] == (60, 90)
    assert path.read_bytes() == original


def test_multipage_tiff_processes_each_frame_and_records_missing_dpi_assumption(tmp_path, monkeypatch):
    path = tmp_path / "receipt.tif"
    Image.new("RGB", (60, 90), "red").save(path, save_all=True,
        append_images=[Image.new("RGB", (60, 90), "blue")])
    seen = []

    def shared(render, count, metrics, *, run_uuid):
        assert count == 2
        assert run_uuid == "multipage-run"
        for index in range(count):
            seen.append(render(index, 300).getpixel((0, 0)))
            metrics.append({"page_number": index + 1, "provenance": {}})
        return ["page one", "page two"]

    monkeypatch.setattr(ingest, "_ocr_rendered_pages", shared)
    metrics = []
    assert ingest.extract_page_text(path, metrics, run_uuid="multipage-run") == ["page one", "page two"]
    assert seen == [(255, 0, 0), (0, 0, 255)]
    assert all(m["provenance"]["source_image"]["dpi_basis"].startswith("assumed") for m in metrics)


def test_failed_image_ocr_pass_is_recorded_and_not_silently_retried(tmp_path, monkeypatch):
    path = tmp_path / "receipt.png"
    Image.new("RGB", (60, 90), "white").save(path)
    monkeypatch.setattr(ingest, "_run_tesseract_candidate", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("engine failure")))
    metrics = []
    with pytest.raises(RuntimeError, match="engine failure"):
        ingest.extract_page_text(path, metrics, run_uuid="failed-image-run")
    assert len(metrics) == 1
    assert metrics[0]["status"] == "failed"
    assert metrics[0]["error_type"] == "RuntimeError"
    assert metrics[0]["run_uuid"] == "failed-image-run"
