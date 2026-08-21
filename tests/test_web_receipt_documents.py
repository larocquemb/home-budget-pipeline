from pathlib import Path

import pytest
from fastapi import HTTPException
from PIL import Image

from home_budget_pipeline.web import app as web_app


class FakeService:
    def __init__(self, evidence=None):
        self.evidence = evidence

    def receipt_evidence(self, evidence_id):
        return self.evidence


def test_authenticated_identity_rejects_missing_proxy_headers():
    with pytest.raises(HTTPException) as exc:
        web_app.authenticated_identity()
    assert exc.value.status_code == 401


def test_authenticated_identity_accepts_oauth2_proxy_email():
    identity = web_app.authenticated_identity(x_auth_request_email="paul@example.ca")
    assert identity["email"] == "paul@example.ca"


def test_receipt_document_path_must_stay_inside_configured_root(tmp_path, monkeypatch):
    root = tmp_path / "receipts"
    root.mkdir()
    monkeypatch.setattr(web_app, "RECEIPT_SOURCE_ROOT", root.resolve())

    with pytest.raises(HTTPException) as exc:
        web_app._receipt_document_path("../secret.pdf")
    assert exc.value.status_code == 404


def test_receipt_document_path_resolves_existing_scan(tmp_path, monkeypatch):
    root = tmp_path / "receipts"
    root.mkdir()
    scan = root / "receipt_0001.pdf"
    scan.write_bytes(b"%PDF-test")
    monkeypatch.setattr(web_app, "RECEIPT_SOURCE_ROOT", root.resolve())

    assert web_app._receipt_document_path("receipt_0001.pdf") == scan.resolve()


def test_receipt_document_path_resolves_electronic_receipt(tmp_path, monkeypatch):
    root = tmp_path / "electronic"
    costco = root / "Costco"
    costco.mkdir(parents=True)
    receipt = costco / "20260726.pdf"
    receipt.write_bytes(b"%PDF-test")
    monkeypatch.setattr(web_app, "ELECTRONIC_RECEIPT_SOURCE_ROOT", root.resolve())

    assert web_app._receipt_document_path("Costco/20260726.pdf", "electronic") == receipt.resolve()


def test_receipt_document_returns_inline_file_response(tmp_path, monkeypatch):
    root = tmp_path / "receipts"
    root.mkdir()
    scan = root / "receipt_0001.pdf"
    scan.write_bytes(b"%PDF-test")
    monkeypatch.setattr(web_app, "RECEIPT_SOURCE_ROOT", root.resolve())
    service = FakeService(
        {"id": 7, "source_reference": "receipt_0001.pdf", "mime_type": "application/pdf"}
    )

    response = web_app.receipt_document(
        evidence_id=7,
        service=service,
        _={"user": "Paul", "email": ""},
    )

    assert Path(response.path) == scan.resolve()
    assert response.media_type == "application/pdf"
    assert response.headers["content-disposition"].startswith("inline;")


def test_receipt_document_404_when_evidence_missing():
    with pytest.raises(HTTPException) as exc:
        web_app.receipt_document(
            evidence_id=99,
            service=FakeService(None),
            _={"user": "Paul", "email": ""},
        )
    assert exc.value.status_code == 404


def test_receipt_preview_embeds_image():
    preview = web_app._receipt_preview_html(
        9,
        {"source_reference": "receipt.jpg", "mime_type": "image/jpeg"},
    )

    assert '<img class="receipt-preview receipt-preview-image"' in preview
    assert f'src="{web_app.BASE_PATH}/evidence/9/document"' in preview
    assert "receipt-preview-viewport" in preview
    assert "data-hover-zoom" in preview
    assert "Open original receipt" in preview


def test_receipt_crop_detects_dense_edges_and_ignores_sparse_scanner_noise():
    image = Image.new("RGB", (200, 300), "white")
    for x in range(70, 130):
        for y in range(40, 260):
            image.putpixel((x, y), (180, 180, 180))
    image.putpixel((1, 1), (0, 0, 0))
    image.putpixel((198, 298), (0, 0, 0))

    cropped = web_app._crop_receipt_image(image, threshold=245, padding=5)

    assert cropped.size == (70, 230)


def test_pdf_preview_endpoint_returns_cached_png(tmp_path, monkeypatch):
    root = tmp_path / "receipts"
    root.mkdir()
    scan = root / "receipt.pdf"
    scan.write_bytes(b"%PDF-test")
    monkeypatch.setattr(web_app, "RECEIPT_SOURCE_ROOT", root.resolve())
    monkeypatch.setattr(web_app, "_render_pdf_page_png", lambda path, page: b"png-data")
    service = FakeService(
        {"id": 7, "source_reference": "receipt.pdf", "mime_type": "application/pdf"}
    )

    response = web_app.receipt_preview_image(
        evidence_id=7,
        page_number=0,
        service=service,
        _={"user": "Paul", "email": ""},
    )

    assert response.body == b"png-data"
    assert response.media_type == "image/png"
    assert response.headers["cache-control"] == "private, max-age=3600"
