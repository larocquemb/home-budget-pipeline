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
from .render import esc, money, page, pager, table

BASE_PATH = os.getenv("LEDGER_BASE_PATH", "/ledger").rstrip("/") or "/ledger"
RECEIPT_SOURCE_ROOT = Path(os.getenv("RECEIPT_SOURCE_ROOT", "/data/receipts/raw/scanned/inbox")).resolve()

app = FastAPI(title="BrownRook Ledger", version="0.4.0")


def _header_text(value: object) -> Optional[str]:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


def authenticated_identity(
    x_forwarded_user: Optional[str] = Header(default=None),
    x_forwarded_email: Optional[str] = Header(default=None),
    x_auth_request_user: Optional[str] = Header(default=None),
    x_auth_request_email: Optional[str] = Header(default=None),
) -> dict[str, str]:
    forwarded_user = _header_text(x_forwarded_user)
    forwarded_email = _header_text(x_forwarded_email)
    auth_user = _header_text(x_auth_request_user)
    auth_email = _header_text(x_auth_request_email)
    user = auth_user or forwarded_user
    email = auth_email or forwarded_email
    if not user and not email:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authenticated identity header missing")
    return {"user": user or email or "unknown", "email": email or ""}


def query_service() -> LedgerQueryService:
    return LedgerQueryService()


def _optional_bool_query(value: Optional[str]) -> Optional[bool]:
    if value is None or value.strip() == "":
        return None
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise HTTPException(status_code=422, detail="requires_review must be true or false")


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
    return authenticated_identity(x_forwarded_user, x_forwarded_email, x_auth_request_user, x_auth_request_email)


@app.get(f"{BASE_PATH}/api/analytics/expenses")
def api_analytics_expenses(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), source: Optional[str] = None, merchant: Optional[str] = None, requires_review: Optional[str] = None, service: LedgerQueryService = Depends(query_service), _: dict[str, str] = Depends(authenticated_identity)):
    return service.analytics_expenses(limit=limit, offset=offset, source=source, merchant=merchant, requires_review=_optional_bool_query(requires_review))


@app.get(f"{BASE_PATH}/api/analytics/category-spend")
def api_category_spend(limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0), service: LedgerQueryService = Depends(query_service), _: dict[str, str] = Depends(authenticated_identity)):
    return service.category_spend(limit=limit, offset=offset)


@app.get(f"{BASE_PATH}/api/review-queue")
def api_review_queue(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), service: LedgerQueryService = Depends(query_service), _: dict[str, str] = Depends(authenticated_identity)):
    return service.review_queue(limit=limit, offset=offset)


@app.get(f"{BASE_PATH}/api/extraction-audit")
def api_extraction_audit(limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0), status_filter: Optional[str] = Query(None, alias="status"), max_items: int = Query(4, ge=0, le=1000), service: LedgerQueryService = Depends(query_service), _: dict[str, str] = Depends(authenticated_identity)):
    return service.extraction_audit(limit=limit, offset=offset, status=status_filter or None, max_items=max_items)


@app.get(f"{BASE_PATH}/api/expenses/{{expense_pk}}")
def api_expense_detail(expense_pk: int, service: LedgerQueryService = Depends(query_service), _: dict[str, str] = Depends(authenticated_identity)):
    result = service.expense_detail(expense_pk)
    if result is None:
        raise HTTPException(status_code=404, detail="expense not found")
    return result


@app.get(f"{BASE_PATH}/api/duplicates")
def api_duplicates(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), service: LedgerQueryService = Depends(query_service), _: dict[str, str] = Depends(authenticated_identity)):
    return service.pending_duplicates(limit=limit, offset=offset)


@app.get(f"{BASE_PATH}/api/transactions")
def api_transactions(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), service: LedgerQueryService = Depends(query_service), _: dict[str, str] = Depends(authenticated_identity)):
    return service.transaction_reconciliation(limit=limit, offset=offset)


@app.get(f"{BASE_PATH}/api/receipt-processing")
def api_receipt_processing(limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0), service: LedgerQueryService = Depends(query_service), _: dict[str, str] = Depends(authenticated_identity)):
    return service.receipt_processing_status(limit=limit, offset=offset)


@app.get(f"{BASE_PATH}/api/evidence/{{evidence_id}}")
def api_evidence(evidence_id: int, service: LedgerQueryService = Depends(query_service), _: dict[str, str] = Depends(authenticated_identity)):
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
def receipt_document(evidence_id: int, service: LedgerQueryService = Depends(query_service), _: dict[str, str] = Depends(authenticated_identity)):
    evidence = service.receipt_evidence(evidence_id)
    if evidence is None:
        raise HTTPException(status_code=404, detail="receipt evidence not found")
    source_reference = evidence.get("source_reference")
    if not source_reference:
        raise HTTPException(status_code=404, detail="receipt document not available")
    path = _receipt_document_path(str(source_reference))
    media_type = evidence.get("mime_type") or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=path.name, content_disposition_type="inline")


def _receipt_preview(evidence_id: int, evidence: dict[str, object]) -> str:
    source_reference = evidence.get("source_reference")
    if not source_reference:
        return '<p class="muted">Receipt document not available.</p>'
    document_url = f"{BASE_PATH}/evidence/{evidence_id}/document"
    media_type = evidence.get("mime_type") or mimetypes.guess_type(str(source_reference))[0] or ""
    if str(media_type).lower() == "application/pdf":
        preview = f'<iframe class="receipt-preview receipt-preview-pdf" src="{document_url}" title="Receipt PDF" loading="lazy"></iframe>'
    elif str(media_type).lower().startswith("image/"):
        preview = f'<img class="receipt-preview receipt-preview-image" src="{document_url}" alt="Receipt image" loading="lazy">'
    else:
        preview = '<p class="muted">Preview is not available for this file type.</p>'
    return preview + f'<p><a href="{document_url}" target="_blank" rel="noopener">Open original receipt</a></p>'


@app.get(BASE_PATH, response_class=HTMLResponse)
@app.get(f"{BASE_PATH}/", response_class=HTMLResponse)
def ledger_home(identity: dict[str, str] = Depends(authenticated_identity)) -> str:
    body = f"""
<p>Read-only access to canonical expenses, SQL analytics views, receipt evidence, and review workflows.</p>
<div class="grid">
<div class="card"><h2><a href="{BASE_PATH}/expenses">Expenses</a></h2><p>Browse and filter canonical expenses, then drill into line items and receipts.</p></div>
<div class="card"><h2><a href="{BASE_PATH}/category-spend">Category spend</a></h2><p>Inspect the KAN-71 category-spend analytics view.</p></div>
<div class="card"><h2><a href="{BASE_PATH}/review-queue">Review queue</a></h2><p>See canonical expenses requiring extraction, reconciliation, or quality review.</p></div>
<div class="card"><h2><a href="{BASE_PATH}/extraction-audit">Extraction audit</a></h2><p>Find receipts with suspiciously few canonical line items.</p></div>
<div class="card"><h2><a href="{BASE_PATH}/duplicates">Duplicates</a></h2><p>Inspect unresolved KAN-77 receipt duplicate candidates.</p></div>
<div class="card"><h2><a href="{BASE_PATH}/transactions">Transactions</a></h2><p>Inspect KAN-78 matched, ambiguous, and unmatched financial transactions.</p></div>
<div class="card"><h2><a href="{BASE_PATH}/receipt-processing">Receipt processing</a></h2><p>See KAN-82 processing attempts, failures, and receipts requiring review.</p></div>
</div>"""
    return page("Dashboard", body, base_path=BASE_PATH, identity=identity)


@app.get(f"{BASE_PATH}/expenses", response_class=HTMLResponse)
def expenses_page(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), source: Optional[str] = None, merchant: Optional[str] = None, requires_review: Optional[str] = None, service: LedgerQueryService = Depends(query_service), identity: dict[str, str] = Depends(authenticated_identity)) -> str:
    review_filter = _optional_bool_query(requires_review)
    result = service.analytics_expenses(limit=limit, offset=offset, source=source, merchant=merchant, requires_review=review_filter)
    review_value = "" if review_filter is None else str(review_filter).lower()
    body = f"""
<form class="toolbar" method="get">
<label>Source<input name="source" value="{esc(source)}"></label>
<label>Merchant<input name="merchant" value="{esc(merchant)}"></label>
<label>Review<select name="requires_review"><option value="">All</option><option value="true"{' selected' if review_value == 'true' else ''}>Needs review</option><option value="false"{' selected' if review_value == 'false' else ''}>No review</option></select></label>
<label>Rows<input name="limit" type="number" min="1" max="200" value="{limit}"></label>
<button type="submit">Filter</button>
</form>"""
    body += table(result.rows, (("expense_pk", "Expense"), ("source", "Source"), ("order_date", "Date"), ("store_name", "Merchant"), ("account_name", "Account"), ("expense_total", "Total"), ("extraction_status", "Extraction"), ("requires_review", "Review")), links={"expense_pk": f"{BASE_PATH}/expenses/{{value}}"}, money_columns={"expense_total"})
    body += pager(f"{BASE_PATH}/expenses", limit=limit, offset=offset, row_count=len(result.rows), query={"source": source, "merchant": merchant, "requires_review": review_value})
    return page("Expenses", body, base_path=BASE_PATH, identity=identity)


@app.get(f"{BASE_PATH}/category-spend", response_class=HTMLResponse)
def category_spend_page(limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0), service: LedgerQueryService = Depends(query_service), identity: dict[str, str] = Depends(authenticated_identity)) -> str:
    result = service.category_spend(limit=limit, offset=offset)
    body = table(result.rows, (("expense_date", "Date"), ("category_group_name", "Group"), ("budget_category", "Category"), ("category_amount", "Amount"), ("item_count", "Items"), ("expense_count", "Expenses")), money_columns={"category_amount"})
    body += pager(f"{BASE_PATH}/category-spend", limit=limit, offset=offset, row_count=len(result.rows))
    return page("Category spend", body, base_path=BASE_PATH, identity=identity)


@app.get(f"{BASE_PATH}/review-queue", response_class=HTMLResponse)
def review_queue_page(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), service: LedgerQueryService = Depends(query_service), identity: dict[str, str] = Depends(authenticated_identity)) -> str:
    result = service.review_queue(limit=limit, offset=offset)
    body = table(result.rows, (("expense_pk", "Expense"), ("source", "Source"), ("order_date", "Date"), ("store_name", "Merchant"), ("expense_total", "Total"), ("extraction_status", "Extraction"), ("data_quality_status", "Quality"), ("data_quality_violation_count", "Violations")), links={"expense_pk": f"{BASE_PATH}/expenses/{{value}}"}, money_columns={"expense_total"})
    body += pager(f"{BASE_PATH}/review-queue", limit=limit, offset=offset, row_count=len(result.rows))
    return page("Review queue", body, base_path=BASE_PATH, identity=identity)


@app.get(f"{BASE_PATH}/extraction-audit", response_class=HTMLResponse)
def extraction_audit_page(limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0), status_filter: Optional[str] = Query(None, alias="status"), max_items: int = Query(4, ge=0, le=1000), service: LedgerQueryService = Depends(query_service), identity: dict[str, str] = Depends(authenticated_identity)) -> str:
    status_value = status_filter.strip() if isinstance(status_filter, str) else ""
    result = service.extraction_audit(limit=limit, offset=offset, status=status_value or None, max_items=max_items)
    body = f"""
<form class="toolbar" method="get">
<label>Status<select name="status"><option value="">All</option><option value="complete"{' selected' if status_value == 'complete' else ''}>Complete</option><option value="review"{' selected' if status_value == 'review' else ''}>Review</option><option value="unreadable"{' selected' if status_value == 'unreadable' else ''}>Unreadable</option></select></label>
<label>Maximum items<input name="max_items" type="number" min="0" max="1000" value="{max_items}"></label>
<label>Rows<input name="limit" type="number" min="1" max="500" value="{limit}"></label>
<button type="submit">Filter</button>
</form>"""
    body += table(result.rows, (("expense_pk", "Expense"), ("evidence_id", "Evidence"), ("source_reference", "Receipt"), ("order_date", "Date"), ("store_name", "Merchant"), ("expense_total", "Total"), ("extraction_status", "Extraction"), ("item_count", "Items")), links={"expense_pk": f"{BASE_PATH}/expenses/{{value}}", "evidence_id": f"{BASE_PATH}/evidence/{{value}}"}, money_columns={"expense_total"})
    body += pager(f"{BASE_PATH}/extraction-audit", limit=limit, offset=offset, row_count=len(result.rows), query={"status": status_value, "max_items": max_items})
    return page("Extraction audit", body, base_path=BASE_PATH, identity=identity)


@app.get(f"{BASE_PATH}/duplicates", response_class=HTMLResponse)
def duplicates_page(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), service: LedgerQueryService = Depends(query_service), identity: dict[str, str] = Depends(authenticated_identity)) -> str:
    result = service.pending_duplicates(limit=limit, offset=offset)
    body = table(result.rows, (("id", "Candidate"), ("left_evidence_id", "Left evidence"), ("right_evidence_id", "Right evidence"), ("score", "Score"), ("disposition", "Disposition"), ("item_similarity", "Item similarity"), ("reasons", "Reasons")), links={"left_evidence_id": f"{BASE_PATH}/evidence/{{value}}", "right_evidence_id": f"{BASE_PATH}/evidence/{{value}}"})
    body += pager(f"{BASE_PATH}/duplicates", limit=limit, offset=offset, row_count=len(result.rows))
    return page("Duplicate review", body, base_path=BASE_PATH, identity=identity)


@app.get(f"{BASE_PATH}/transactions", response_class=HTMLResponse)
def transactions_page(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), service: LedgerQueryService = Depends(query_service), identity: dict[str, str] = Depends(authenticated_identity)) -> str:
    result = service.transaction_reconciliation(limit=limit, offset=offset)
    body = table(result.rows, (("transaction_id", "Transaction"), ("transaction_date", "Date"), ("description", "Description"), ("amount", "Amount"), ("payer", "Payer"), ("outcome", "Outcome"), ("score", "Score"), ("expense_pk", "Expense")), links={"expense_pk": f"{BASE_PATH}/expenses/{{value}}"}, money_columns={"amount"})
    body += pager(f"{BASE_PATH}/transactions", limit=limit, offset=offset, row_count=len(result.rows))
    return page("Transaction reconciliation", body, base_path=BASE_PATH, identity=identity)


@app.get(f"{BASE_PATH}/receipt-processing", response_class=HTMLResponse)
def receipt_processing_page(limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0), service: LedgerQueryService = Depends(query_service), identity: dict[str, str] = Depends(authenticated_identity)) -> str:
    result = service.receipt_processing_status(limit=limit, offset=offset)
    body = table(result.rows, (("source_reference", "Receipt"), ("status", "Status"), ("attempts", "Attempts"), ("last_attempted_at", "Last attempt"), ("completed_at", "Completed"), ("last_error", "Last error")))
    body += pager(f"{BASE_PATH}/receipt-processing", limit=limit, offset=offset, row_count=len(result.rows))
    return page("Receipt processing", body, base_path=BASE_PATH, identity=identity)


@app.get(f"{BASE_PATH}/expenses/{{expense_pk}}", response_class=HTMLResponse)
def expense_page(expense_pk: int, service: LedgerQueryService = Depends(query_service), identity: dict[str, str] = Depends(authenticated_identity)) -> str:
    expense = service.expense_detail(expense_pk)
    if expense is None:
        raise HTTPException(status_code=404, detail="expense not found")
    detail = f"""<div class="card"><div class="grid">
<div><strong>Merchant</strong><br>{esc(expense.get('store_name'))}</div>
<div><strong>Date</strong><br>{esc(expense.get('order_date') or expense.get('transaction_datetime'))}</div>
<div><strong>Source</strong><br>{esc(expense.get('source'))}</div>
<div><strong>Total</strong><br>{money(expense.get('expense_total'))}</div>
<div><strong>Account</strong><br>{esc(expense.get('account_name'))}</div>
<div><strong>Requires review</strong><br>{esc(expense.get('requires_review'))}</div>
</div></div>"""
    detail += "<h2>Line items</h2>" + table(expense.get("items", ()), (("item_name", "Item"), ("budget_category", "Category"), ("category_group_name", "Group"), ("line_total", "Amount"), ("category_source", "Category source"), ("category_confidence", "Confidence")), money_columns={"line_total"})
    detail += "<h2>Receipt evidence</h2>" + table(expense.get("evidence", ()), (("id", "Evidence"), ("evidence_type", "Type"), ("source_reference", "Source file"), ("transaction_datetime", "Date/time"), ("total", "Total"), ("extraction_status", "Extraction")), links={"id": f"{BASE_PATH}/evidence/{{value}}"}, money_columns={"total"})
    return page(f"Expense {expense_pk}", detail, base_path=BASE_PATH, identity=identity)


@app.get(f"{BASE_PATH}/evidence/{{evidence_id}}", response_class=HTMLResponse)
def evidence_page(evidence_id: int, service: LedgerQueryService = Depends(query_service), identity: dict[str, str] = Depends(authenticated_identity)) -> str:
    evidence = service.receipt_evidence(evidence_id)
    if evidence is None:
        raise HTTPException(status_code=404, detail="receipt evidence not found")
    expense_link = f'<a href="{BASE_PATH}/expenses/{evidence["expense_pk"]}">{evidence["expense_pk"]}</a>' if evidence.get("expense_pk") else ""
    body = f"""<div class="card"><div class="grid">
<div><strong>Type</strong><br>{esc(evidence.get('evidence_type'))}</div>
<div><strong>Merchant</strong><br>{esc(evidence.get('merchant'))}</div>
<div><strong>Date/time</strong><br>{esc(evidence.get('transaction_datetime'))}</div>
<div><strong>Total</strong><br>{money(evidence.get('total'))}</div>
<div><strong>Extraction</strong><br>{esc(evidence.get('extraction_status'))} {esc(evidence.get('extraction_confidence'))}</div>
<div><strong>Canonical expense</strong><br>{expense_link}</div>
<div><strong>Source</strong><br>{esc(evidence.get('source_reference'))}</div>
</div></div>
<h2>Receipt</h2>{_receipt_preview(evidence_id, evidence)}
<h2>Extracted text</h2><pre>{html.escape(str(evidence.get('raw_text') or ''))}</pre>"""
    return page(f"Receipt evidence {evidence_id}", body, base_path=BASE_PATH, identity=identity)


def main() -> None:
    import uvicorn
    uvicorn.run("home_budget_pipeline.web.app:app", host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8080")))


if __name__ == "__main__":
    main()
