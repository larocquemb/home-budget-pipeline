from pathlib import Path

import pytest
from fastapi import HTTPException

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
