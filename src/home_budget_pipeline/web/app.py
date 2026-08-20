"""BrownRook Ledger read-only web/API application served behind oauth2-proxy."""

from __future__ import annotations

import html
import mimetypes
import os
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.responses import FileResponse, HTMLResponse

from .queries import LedgerQueryService

BASE_PATH = os.getenv("LEDGER_BASE_PATH", "/ledger").rstrip("/") or "/ledger"
RECEIPT_SOURCE_ROOT = Path(
    os.getenv("RECEIPT_SOURCE_ROOT", "/data/receipts/raw/scanned/inbox")
).resolve()

app = FastAPI(title="BrownRook Ledger", version="0.2.0")


def authenticated_identity(
    x_forwarded_user: Optional[str] = Header(default=None),
    x_forwarded_email: Optional[str] = Header(default=None),
    x_auth_request_user: Optional[str] = Header(default=None),
    x_auth_request_email: Optional[str] = Header(default=None),
) -> dict[str, str]:
    user = x_auth_request_user or x_forwarded_user
    email = x_auth_request_email or x_forwarded_email
    if not user and not email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authenticated identity header missing",
        )
    return {"user": user or email or "unknown", "email": email or ""}


def query_service() -> LedgerQueryService:
    return LedgerQueryService()


@app.get(f"{BASE_PATH}/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "ledger"}


@app.get(f"{BASE_PATH}/ready")
def ready(service: LedgerQueryService = Depends(query_service)) -> dict[str, str]:
    service.analytics_expenses(limit=1)
    return {"status": "ready", "service": "ledger"}


@app.get(f"{BASE_PATH}/api/me")
def me(
    x_forwarded_user: Optional[str] = Header(default=None),
    x_forwarded_email: Optional[str] = Header(default=None),
    x_auth_request_user: Optional[str] = Header(default=None),
    x_auth_request_email: Optional[str] = Header(default=None),
) -> dict[str, str]:
    return authenticated_identity(
        x_forwarded_user=x_forwarded_user,
        x_forwarded_email=x_forwarded_email,
        x_auth_request_user=x_auth_request_user,
        x_auth_request_email=x_auth_request_email,
    )


@app.get(f"{BASE_PATH}/api/analytics/expenses")
def api_analytics_expenses(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    source: Optional[str] = None,
    merchant: Optional[str] = None,
    requires_review: Optional[bool] = None,
    service: LedgerQueryService = Depends(query_service),
    _: dict[str, str] = Depends(authenticated_identity),
):
    return service.analytics_expenses(
        limit=limit,
        offset=offset,
        source=source,
        merchant=merchant,
        requires_review=requires_review,
    )


@app.get(f"{BASE_PATH}/api/analytics/category-spend")
def api_category_spend(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    service: LedgerQueryService = Depends(query_service),
    _: dict[str, str] = Depends(authenticated_identity),
):
    return service.category_spend(limit=limit, offset=offset)


@app.get(f"{BASE_PATH}/api/review-queue")
def api_review_queue(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    service: LedgerQueryService = Depends(query_service),
    _: dict[str, str] = Depends(authenticated_identity),
):
    return service.review_queue(limit=limit, offset=offset)


@app.get(f"{BASE_PATH}/api/expenses/{{expense_pk}}")
def api_expense_detail(
    expense_pk: int,
    service: LedgerQueryService = Depends(query_service),
    _: dict[str, str] = Depends(authenticated_identity),
):
    result = service.expense_detail(expense_pk)
    if result is None:
        raise HTTPException(status_code=404, detail="expense not found")
    return result


@app.get(f"{BASE_PATH}/api/duplicates")
def api_duplicates(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    service: LedgerQueryService = Depends(query_service),
    _: dict[str, str] = Depends(authenticated_identity),
):
    return service.pending_duplicates(limit=limit, offset=offset)


@app.get(f"{BASE_PATH}/api/transactions")
def api_transactions(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    service: LedgerQueryService = Depends(query_service),
    _: dict[str, str] = Depends(authenticated_identity),
):
    return service.transaction_reconciliation(limit=limit, offset=offset)


@app.get(f"{BASE_PATH}/api/evidence/{{evidence_id}}")
def api_evidence(
    evidence_id: int,
    service: LedgerQueryService = Depends(query_service),
    _: dict[str, str] = Depends(authenticated_identity),
):
    result = service.receipt_evidence(evidence_id)
    if result is None:
        raise HTTPException(status_code=404, detail="receipt evidence not found")
    return result


def _receipt_document_path(source_reference: str) -> Path:
    candidate = (RECEIPT_SOURCE_ROOT / source_reference).resolve()
    if candidate != RECEIPT_SOURCE_ROOT and RECEIPT_SOURCE_ROOT not in candidate.parents:
        raise HTTPException(status_code=404, detail="receipt document not found")
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="receipt document not found")
    return candidate


@app.get(f"{BASE_PATH}/evidence/{{evidence_id}}/document")
def receipt_document(
    evidence_id: int,
    service: LedgerQueryService = Depends(query_service),
    _: dict[str, str] = Depends(authenticated_identity),
):
    evidence = service.receipt_evidence(evidence_id)
    if evidence is None:
        raise HTTPException(status_code=404, detail="receipt evidence not found")
    source_reference = evidence.get("source_reference")
    if not source_reference:
        raise HTTPException(status_code=404, detail="receipt document not available")
    path = _receipt_document_path(str(source_reference))
    media_type = evidence.get("mime_type") or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(
        path,
        media_type=media_type,
        filename=path.name,
        content_disposition_type="inline",
    )


@app.get(BASE_PATH, response_class=HTMLResponse)
@app.get(f"{BASE_PATH}/", response_class=HTMLResponse)
def ledger_home(
    identity: dict[str, str] = Depends(authenticated_identity),
) -> str:
    display = html.escape(identity["email"] or identity["user"])
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <title>BrownRook Ledger</title>
    <style>
      body {{ font-family: system-ui, sans-serif; margin: 2rem; max-width: 70rem; }}
      nav a {{ margin-right: 1.25rem; }}
      code {{ background: #eee; padding: .1rem .3rem; }}
    </style>
  </head>
  <body>
    <main>
      <h1>BrownRook Ledger</h1>
      <p>Signed in as {display}</p>
      <p>Read-only access to canonical budget analytics and review workflows.</p>
      <nav>
        <a href="{BASE_PATH}/api/analytics/expenses">Expenses</a>
        <a href="{BASE_PATH}/api/analytics/category-spend">Category spend</a>
        <a href="{BASE_PATH}/api/review-queue">Review queue</a>
        <a href="{BASE_PATH}/api/duplicates">Duplicate candidates</a>
        <a href="{BASE_PATH}/api/transactions">Transactions</a>
      </nav>
      <p>Receipt evidence endpoints include an on-demand <code>/evidence/&lt;id&gt;/document</code> view for PDFs and images.</p>
    </main>
  </body>
</html>"""


def main() -> None:
    import uvicorn

    uvicorn.run(
        "home_budget_pipeline.web.app:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8080")),
    )


if __name__ == "__main__":
    main()
