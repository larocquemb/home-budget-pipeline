"""Receipt-first entry point for the BrownRook Ledger web application.

The canonical receipt resource is keyed by the source SHA-256 from ingest.receipts.
Existing expense routes remain available for accounting-only records, while receipt-backed
expense/evidence detail URLs redirect to the stable receipt URL.
"""

from __future__ import annotations

import html
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .app import (
    BASE_PATH,
    _post_ocr_text,
    _receipt_preview_html,
    app,
    authenticated_identity,
    ledger_home,
    query_service,
)
from .queries import LedgerQueryService, Page
from .render import esc, money, page, pager

_RECEIPT_KEY_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_EXPENSE_DETAIL_RE = re.compile(rf"^{re.escape(BASE_PATH)}/expenses/(\d+)$")
_EVIDENCE_DETAIL_RE = re.compile(rf"^{re.escape(BASE_PATH)}/evidence/(\d+)$")


def receipt_url(source_sha256: str) -> str:
    """Return the canonical URL for a receipt identity."""
    return f"{BASE_PATH}/receipts/{quote(source_sha256, safe='')}"


def _receipt_list(service: LedgerQueryService, *, limit: int, offset: int) -> Page:
    count_rows = service._fetch("SELECT COUNT(*) AS total_count FROM ingest.receipts")
    total_count = int(count_rows[0]["total_count"]) if count_rows else 0
    rows = service._fetch(
        """
        SELECT
            r.source_sha256,
            r.source_reference,
            s.status AS processing_status,
            s.attempts,
            re.id AS evidence_id,
            re.expense_pk,
            COALESCE(re.merchant, e.store_name) AS merchant,
            COALESCE(re.transaction_datetime, e.transaction_datetime,
                     e.order_date::timestamp) AS transaction_datetime,
            COALESCE(re.total, e.expense_total) AS total,
            COALESCE(re.extraction_status, e.extraction_status) AS extraction_status,
            re.extraction_confidence
        FROM ingest.receipts r
        LEFT JOIN ingest.receipt_processing_status s USING (source_sha256)
        LEFT JOIN LATERAL (
            SELECT id, expense_pk, merchant, transaction_datetime, total,
                   extraction_status, extraction_confidence
            FROM budget.receipt_evidence
            WHERE source_sha256 = r.source_sha256
            ORDER BY is_primary_source DESC, id
            LIMIT 1
        ) re ON TRUE
        LEFT JOIN budget.expenses e ON e.id = re.expense_pk
        ORDER BY COALESCE(re.transaction_datetime, e.transaction_datetime,
                          e.order_date::timestamp) DESC NULLS LAST,
                 r.source_reference DESC
        LIMIT %s OFFSET %s
        """,
        (limit, offset),
    )
    return Page(rows, limit, offset, total_count)


def _receipt_detail(service: LedgerQueryService, source_sha256: str) -> dict[str, Any] | None:
    if not _RECEIPT_KEY_RE.fullmatch(source_sha256):
        return None
    rows = service._fetch(
        """
        SELECT r.source_sha256, r.source_reference,
               r.first_discovered_at, r.updated_at,
               s.status AS processing_status, s.attempts, s.last_error,
               s.first_attempted_at, s.last_attempted_at, s.completed_at,
               re.id AS evidence_id, re.expense_pk
        FROM ingest.receipts r
        LEFT JOIN ingest.receipt_processing_status s USING (source_sha256)
        LEFT JOIN LATERAL (
            SELECT id, expense_pk
            FROM budget.receipt_evidence
            WHERE source_sha256 = r.source_sha256
            ORDER BY is_primary_source DESC, id
            LIMIT 1
        ) re ON TRUE
        WHERE r.source_sha256 = %s
        """,
        (source_sha256,),
    )
    if not rows:
        return None
    receipt = dict(rows[0])
    evidence_id = receipt.get("evidence_id")
    evidence = service.receipt_evidence(int(evidence_id)) if evidence_id is not None else None
    expense_pk = receipt.get("expense_pk") or (evidence or {}).get("expense_pk")
    expense = service.expense_detail(int(expense_pk)) if expense_pk is not None else None
    receipt["evidence"] = evidence
    receipt["expense"] = expense
    return receipt


def _receipt_for_expense(service: LedgerQueryService, expense_pk: int) -> str | None:
    rows = service._fetch(
        """
        SELECT source_sha256
        FROM budget.receipt_evidence
        WHERE expense_pk = %s
        ORDER BY is_primary_source DESC, id
        LIMIT 1
        """,
        (expense_pk,),
    )
    return str(rows[0]["source_sha256"]) if rows else None


def _receipt_for_evidence(service: LedgerQueryService, evidence_id: int) -> str | None:
    rows = service._fetch(
        "SELECT source_sha256 FROM budget.receipt_evidence WHERE id = %s",
        (evidence_id,),
    )
    return str(rows[0]["source_sha256"]) if rows else None


@app.middleware("http")
async def receipt_first_redirects(request: Request, call_next):
    """Make receipt-backed detail URLs resolve to the canonical receipt resource."""
    if request.method == "GET":
        path = request.url.path
        expense_match = _EXPENSE_DETAIL_RE.fullmatch(path)
        if expense_match:
            source_sha256 = _receipt_for_expense(LedgerQueryService(), int(expense_match.group(1)))
            if source_sha256:
                return RedirectResponse(receipt_url(source_sha256), status_code=307)
        evidence_match = _EVIDENCE_DETAIL_RE.fullmatch(path)
        if evidence_match:
            source_sha256 = _receipt_for_evidence(LedgerQueryService(), int(evidence_match.group(1)))
            if source_sha256:
                return RedirectResponse(receipt_url(source_sha256), status_code=307)
        if path in {BASE_PATH, f"{BASE_PATH}/"}:
            return RedirectResponse(f"{BASE_PATH}/receipts", status_code=307)
    return await call_next(request)


@app.get(f"{BASE_PATH}/dashboard", response_class=HTMLResponse)
def dashboard(identity: dict[str, str] = Depends(authenticated_identity)) -> str:
    """Preserve access to the original accounting/review dashboard."""
    return ledger_home(identity)


@app.get(f"{BASE_PATH}/api/receipts/{{source_sha256}}")
def api_receipt_detail(
    source_sha256: str,
    service: LedgerQueryService = Depends(query_service),
    _: dict[str, str] = Depends(authenticated_identity),
):
    result = _receipt_detail(service, source_sha256)
    if result is None:
        raise HTTPException(status_code=404, detail="receipt not found")
    return result


@app.get(f"{BASE_PATH}/receipts", response_class=HTMLResponse)
def receipts_page(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    service: LedgerQueryService = Depends(query_service),
    identity: dict[str, str] = Depends(authenticated_identity),
) -> str:
    result = _receipt_list(service, limit=limit, offset=offset)
    total = result.total_count or 0
    noun = "receipt" if total == 1 else "receipts"
    rows: list[str] = []
    for receipt in result.rows:
        source_sha256 = str(receipt.get("source_sha256") or "")
        source_reference = str(receipt.get("source_reference") or "")
        label = Path(source_reference).name or source_reference or source_sha256[:12]
        rows.append(
            "<tr>"
            f'<td><a href="{receipt_url(source_sha256)}">{esc(label)}</a><br><small class="muted">{esc(source_reference)}</small></td>'
            f"<td>{esc(receipt.get('merchant'))}</td>"
            f"<td>{esc(receipt.get('transaction_datetime'))}</td>"
            f'<td class="num">{money(receipt.get("total"))}</td>'
            f"<td>{esc(receipt.get('processing_status'))}</td>"
            f"<td>{esc(receipt.get('extraction_status'))}</td>"
            "</tr>"
        )
    body = (
        f'<div class="toolbar"><span><strong>{total:,}</strong> {noun}</span>'
        f'<span><a href="{BASE_PATH}/dashboard">Accounting &amp; review dashboard</a></span></div>'
        "<table><thead><tr><th>Receipt</th><th>Merchant</th><th>Date/time</th>"
        "<th>Total</th><th>Processing</th><th>Extraction</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )
    body += pager(
        f"{BASE_PATH}/receipts",
        limit=limit,
        offset=offset,
        row_count=len(result.rows),
        total_count=result.total_count,
    )
    return page("Receipts", body, base_path=BASE_PATH, identity=identity)


@app.get(f"{BASE_PATH}/receipts/{{source_sha256}}", response_class=HTMLResponse)
def receipt_page(
    source_sha256: str,
    service: LedgerQueryService = Depends(query_service),
    identity: dict[str, str] = Depends(authenticated_identity),
) -> str:
    receipt = _receipt_detail(service, source_sha256)
    if receipt is None:
        raise HTTPException(status_code=404, detail="receipt not found")

    evidence = receipt.get("evidence") or {}
    expense = receipt.get("expense") or {}
    source_reference = str(receipt.get("source_reference") or "")
    filename = Path(source_reference).name or source_reference
    evidence_id = receipt.get("evidence_id")
    expense_pk = receipt.get("expense_pk") or expense.get("expense_pk")

    canonical_expense = (
        f'<a href="{BASE_PATH}/api/expenses/{expense_pk}">Expense {esc(expense_pk)} (API)</a>'
        if expense_pk
        else '<span class="muted">Not created</span>'
    )
    header = f"""<div class="card"><div class="grid">
<div><strong>Receipt</strong><br>{esc(filename)}</div>
<div><strong>Merchant</strong><br>{esc(evidence.get('merchant') or expense.get('store_name'))}</div>
<div><strong>Date/time</strong><br>{esc(evidence.get('transaction_datetime') or expense.get('transaction_datetime') or expense.get('order_date'))}</div>
<div><strong>Total</strong><br>{money(evidence.get('total') or expense.get('expense_total'))}</div>
<div><strong>Processing</strong><br>{esc(receipt.get('processing_status'))}</div>
<div><strong>Extraction</strong><br>{esc(evidence.get('extraction_status') or expense.get('extraction_status'))}</div>
<div><strong>Source</strong><br>{esc(source_reference)}</div>
<div><strong>Accounting record</strong><br>{canonical_expense}</div>
</div></div>"""

    item_rows = []
    for item in expense.get("items", ()):
        item_rows.append(
            "<tr>"
            f"<td>{esc(item.get('item_name'))}</td>"
            f"<td>{esc(item.get('product_description'))}</td>"
            f"<td>{esc(item.get('budget_category'))}</td>"
            f"<td>{esc(item.get('category_group_name'))}</td>"
            f'<td class="num">{money(item.get("line_total"))}</td>'
            f"<td>{esc(item.get('category_source'))}</td>"
            "</tr>"
        )
    items_html = (
        "<h2>Line items</h2><table><thead><tr><th>Receipt item</th><th>Description</th>"
        "<th>Category</th><th>Group</th><th>Amount</th><th>Category source</th></tr></thead><tbody>"
        + "".join(item_rows)
        + "</tbody></table>"
    )

    review_html = ""
    if evidence_id and evidence:
        extracted_text = html.escape(_post_ocr_text(evidence, expense or None))
        ocr_text = html.escape(str(evidence.get("raw_text") or ""))
        review_html = f"""<div class="receipt-review-grid">
<section><h2>Receipt document</h2>{_receipt_preview_html(int(evidence_id), evidence)}</section>
<section><h2>Extracted text</h2><pre class="receipt-text">{extracted_text}</pre>
<details><summary>OCR text</summary><pre class="receipt-text">{ocr_text}</pre></details></section>
</div>"""

    body = (
        f'<p><a href="{BASE_PATH}/receipts">← All receipts</a> · '
        f'<a href="{BASE_PATH}/dashboard">Accounting &amp; review dashboard</a></p>'
        + header
        + review_html
        + items_html
    )
    return page(f"Receipt – {filename}", body, base_path=BASE_PATH, identity=identity)


def main() -> None:
    import uvicorn

    uvicorn.run(
        "home_budget_pipeline.web.receipt_app:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8080")),
    )


if __name__ == "__main__":
    main()
