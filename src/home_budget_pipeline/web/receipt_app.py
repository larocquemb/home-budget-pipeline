"""Receipt-first entry point for the BrownRook Ledger web application."""

from __future__ import annotations

import html
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlparse

from fastapi import Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
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
from .queries import LedgerQueryService, Page, RecommendationConflict, recommendation_candidate
from ..product_labels import product_name, corrected_reading, ocr_variants
from .render import esc, money, page, pager
from . import receipt_graph as _receipt_graph_routes

_RECEIPT_KEY_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_EXPENSE_DETAIL_RE = re.compile(rf"^{re.escape(BASE_PATH)}/expenses/(\d+)$")
_EVIDENCE_DETAIL_RE = re.compile(rf"^{re.escape(BASE_PATH)}/evidence/(\d+)$")
_RECEIPT_SORT_COLUMNS = {
    'expense_id': 're.expense_pk',
    'receipt': 'r.source_reference',
    'merchant': 'COALESCE(re.merchant, e.store_name)',
    'date': 'COALESCE(re.transaction_datetime, e.transaction_datetime, e.order_date::timestamp)',
    'total': 'COALESCE(re.total, e.expense_total)',
    'processing': 's.status',
    'extraction': 'COALESCE(re.extraction_status, e.extraction_status)',
}


def receipt_url(source_sha256: str) -> str:
    return f"{BASE_PATH}/receipts/{quote(source_sha256, safe='')}"


def receipt_graph_url(source_sha256: str) -> str:
    return f"{BASE_PATH}/graph?receipt={quote(source_sha256, safe='')}"


def receipt_label(expense_pk, merchant, receipt_date, total) -> str:
    parts = [f'Expense {expense_pk}' if expense_pk is not None else 'Receipt']
    if merchant:
        parts.append(str(merchant))
    if receipt_date:
        parts.append(str(receipt_date)[:10])
    if total is not None:
        parts.append(f'${float(total):,.2f}')
    return ' · '.join(parts)


def _receipt_list(service: LedgerQueryService, *, limit: int, offset: int,
                  sort: str = 'date', direction: str = 'desc', q: str = '') -> Page:
    sort_column = _RECEIPT_SORT_COLUMNS.get(sort, _RECEIPT_SORT_COLUMNS['date'])
    sort_direction = 'ASC' if direction == 'asc' else 'DESC'
    joins = """
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
    """
    conditions, params = [], []
    # Each word can match a different field (e.g. "Sobeys 2026-02-14").
    for word in q.split():
        pattern = '%' + word.removeprefix('$').replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        conditions.append("""(
            concat_ws(' ', re.expense_pk::text, r.source_reference,
                      COALESCE(re.merchant, e.store_name),
                      COALESCE(re.transaction_datetime, e.transaction_datetime, e.order_date::timestamp)::text,
                      COALESCE(re.total, e.expense_total)::text,
                      s.status, COALESCE(re.extraction_status, e.extraction_status)) ILIKE %s
            OR EXISTS (SELECT 1 FROM budget.expense_items i
                        WHERE i.expense_pk = re.expense_pk
                          AND concat_ws(' ', i.item_name, i.product_description) ILIKE %s)
        )""")
        params.extend((pattern, pattern))
    where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
    count_rows = service._fetch('SELECT COUNT(*) AS total_count' + joins + where, tuple(params))
    total_count = int(count_rows[0]["total_count"]) if count_rows else 0
    rows = service._fetch(
        f"""
        SELECT r.source_sha256, r.source_reference,
               s.status AS processing_status, s.attempts,
               re.id AS evidence_id, re.expense_pk,
               COALESCE(re.merchant, e.store_name) AS merchant,
               COALESCE(re.transaction_datetime, e.transaction_datetime,
                        e.order_date::timestamp) AS transaction_datetime,
               COALESCE(re.total, e.expense_total) AS total,
               COALESCE(re.extraction_status, e.extraction_status) AS extraction_status,
               re.extraction_confidence
         {joins} {where}
         ORDER BY {sort_column} {sort_direction} NULLS LAST,
                  r.source_reference DESC
         LIMIT %s OFFSET %s
        """,
        (*params, limit, offset),
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
    if expense and expense.get("items"):
        enrichment_rows = service._fetch(
            """
            SELECT expense_item_id, provider, search_query, candidate_title,
                   candidate_url, confidence, status, searched_at
              FROM budget.product_enrichment_results
             WHERE expense_item_id = ANY(%s)
            """,
            ([int(item["expense_item_id"]) for item in expense["items"]],),
        )
        enrichment_by_item = {
            int(row["expense_item_id"]): dict(row) for row in enrichment_rows
        }
        from ..product_enrichment import retailer_domain, candidate_evidence
        domain = retailer_domain(expense.get('store_name') or '')
        if domain:
            for item in expense['items']:
                enrichment = enrichment_by_item.get(int(item['expense_item_id']))
                if enrichment and enrichment.get('status') == 'accepted':
                    enrichment['reading_evidence'] = candidate_evidence(item.get('item_name') or '', domain,
                        enrichment.get('candidate_title') or '', enrichment.get('candidate_url') or '')
        recommendation_rows = service._fetch(
            """SELECT DISTINCT ON (c.expense_item_id) c.id,c.expense_item_id,c.run_uuid,
                      c.completed_at,c.payload,a.occurred_at AS accepted_at
                 FROM enrichment.receipt_collaborations c
                 LEFT JOIN LATERAL (SELECT occurred_at FROM lineage.receipt_events
                     WHERE source_sha256=%s AND payload->>'event'='product_recommendation_accepted'
                       AND payload->>'collaboration_id'=c.id::text
                     ORDER BY occurred_at DESC LIMIT 1) a ON TRUE
                WHERE c.expense_item_id=ANY(%s)
                ORDER BY c.expense_item_id,c.completed_at DESC,c.id DESC""",
            (source_sha256, [int(item["expense_item_id"]) for item in expense["items"]]),
        )
        recommendations = {}
        for row in recommendation_rows:
            payload = row['payload']
            decision = payload.get('decision') or {}
            try:
                candidate = recommendation_candidate(payload)
                eligible = True
            except ValueError:
                candidate = {}
                eligible = False
            recommendations[int(row['expense_item_id'])] = {
                'id': row['id'], 'run_uuid': str(row['run_uuid']), 'completed_at': row['completed_at'],
                'disposition': decision.get('disposition'), 'confidence': decision.get('confidence'),
                'candidate_title': candidate.get('title') or decision.get('candidate_title'),
                'candidate_url': candidate.get('url') or decision.get('candidate_url'),
                'evidence': candidate.get('evidence'), 'eligible': eligible,
                'ocr_variants': ocr_variants(payload),
                'accepted_at': row.get('accepted_at'), 'reasons': decision.get('reasons') or [],
            }
        expense["items"] = tuple(
            {
                **dict(item),
                "enrichment": enrichment_by_item.get(int(item["expense_item_id"])),
                "recommendation": recommendations.get(int(item["expense_item_id"])),
            }
            for item in expense["items"]
        )
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


class AcceptRecommendationRequest(BaseModel):
    expense_item_id: int = Field(gt=0)
    collaboration_id: int = Field(gt=0)
    expected_description: str | None = Field(max_length=10000)
    expected_url: str | None = Field(max_length=10000)


@app.post(f"{BASE_PATH}/api/receipts/{{source_sha256}}/accept-recommendation")
def accept_recommendation(source_sha256: str, request: AcceptRecommendationRequest,
                          x_ledger_action: str | None = Header(default=None),
                          service: LedgerQueryService = Depends(query_service),
                          identity: dict[str, str] = Depends(authenticated_identity)):
    if x_ledger_action != 'accept-product-recommendation':
        raise HTTPException(403, 'Recommendation action header missing')
    if not _RECEIPT_KEY_RE.fullmatch(source_sha256):
        raise HTTPException(422, 'Invalid receipt identity')
    try:
        return service.accept_product_recommendation(source_sha256, request.expense_item_id,
            request.collaboration_id, expected_description=request.expected_description,
            expected_url=request.expected_url, actor_user=identity['user'], actor_email=identity.get('email') or None)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RecommendationConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post(f"{BASE_PATH}/api/receipts/{{source_sha256}}/refresh-items")
def refresh_items(source_sha256: str, x_ledger_action: str | None = Header(default=None),
                  service: LedgerQueryService = Depends(query_service),
                  identity: dict[str, str] = Depends(authenticated_identity)):
    if x_ledger_action != 'refresh-receipt-items':
        raise HTTPException(403, 'Receipt refresh action header missing')
    if not _RECEIPT_KEY_RE.fullmatch(source_sha256):
        raise HTTPException(422, 'Invalid receipt identity')
    try:
        return service.refresh_receipt_items(source_sha256, actor_user=identity['user'],
                                              actor_email=identity.get('email') or None)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get(f"{BASE_PATH}/receipts", response_class=HTMLResponse)
def receipts_page(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    sort: str = 'date',
    direction: str = 'desc',
    q: str = '',
    service: LedgerQueryService = Depends(query_service),
    identity: dict[str, str] = Depends(authenticated_identity),
) -> str:
    sort = sort if sort in _RECEIPT_SORT_COLUMNS else 'date'
    direction = 'asc' if direction.lower() == 'asc' else 'desc'
    q = q.strip()
    result = _receipt_list(service, limit=limit, offset=offset, sort=sort, direction=direction, q=q)
    headers = []
    for key, label in [('expense_id', 'Expense ID'), ('receipt', 'Receipt'), ('merchant', 'Merchant'),
                       ('date', 'Date/time'), ('total', 'Total'), ('processing', 'Processing'),
                       ('extraction', 'Extraction')]:
        next_direction = 'desc' if sort == key and direction == 'asc' else 'asc'
        query = {'sort': key, 'direction': next_direction, 'limit': limit}
        if q:
            query['q'] = q
        sort_url = f'{BASE_PATH}/receipts?' + urlencode(query)
        indicator = (' ▲' if direction == 'asc' else ' ▼') if sort == key else ''
        aria_sort = ('ascending' if direction == 'asc' else 'descending') if sort == key else 'none'
        headers.append(f'<th aria-sort="{aria_sort}"><a href="{esc(sort_url)}">{label}{indicator}</a></th>')
    total = result.total_count or 0
    noun = "receipt" if total == 1 else "receipts"
    rows: list[str] = []
    for receipt in result.rows:
        source_sha256 = str(receipt.get("source_sha256") or "")
        source_reference = str(receipt.get("source_reference") or "")
        expense_pk = receipt.get('expense_pk')
        label = receipt_label(expense_pk, receipt.get('merchant'),
                              receipt.get('transaction_datetime'), receipt.get('total'))
        expense_link = (
            f'<a href="{BASE_PATH}/expenses/{esc(expense_pk)}">{esc(expense_pk)}</a>'
            if expense_pk is not None else '<span class="muted">Pending</span>'
        )
        rows.append(
            "<tr>"
            f'<td>{expense_link}</td>'
            f'<td><a href="{receipt_url(source_sha256)}">{esc(label)}</a> · <a href="{receipt_graph_url(source_sha256)}">Processing graph</a><br><small class="muted">{esc(source_reference)}</small></td>'
            f"<td>{esc(receipt.get('merchant'))}</td>"
            f"<td>{esc(receipt.get('transaction_datetime'))}</td>"
            f'<td class="num">{money(receipt.get("total"))}</td>'
            f"<td>{esc(receipt.get('processing_status'))}</td>"
            f"<td>{esc(receipt.get('extraction_status'))}</td>"
            "</tr>"
        )
    body = (
        f'<form class="toolbar" method="get" action="{BASE_PATH}/receipts">'
        f'<input type="hidden" name="sort" value="{esc(sort)}">'
        f'<input type="hidden" name="direction" value="{direction}">'
        f'<label>Search receipts<input type="search" name="q" value="{esc(q)}" '
        'placeholder="Merchant, date, total, filename, expense ID or item" size="48"></label>'
        f'<label>Rows<input name="limit" type="number" min="1" max="500" value="{limit}"></label>'
        '<button type="submit">Search</button>'
        f'<a href="{BASE_PATH}/receipts">Clear</a>'
        f'<span><strong>{total:,}</strong> {"matching " if q else ""}{noun}</span>'
        f'<a href="{BASE_PATH}/dashboard">Accounting &amp; review dashboard</a></form>'
        '<table><thead><tr>' + ''.join(headers) + '</tr></thead><tbody>'
        + ("".join(rows) or '<tr><td colspan="7" class="muted">No receipts match your search.</td></tr>')
        + "</tbody></table>"
    )
    body += pager(
        f"{BASE_PATH}/receipts",
        limit=limit,
        offset=offset,
        row_count=len(result.rows),
        total_count=result.total_count,
        query={'sort': sort, 'direction': direction, 'q': q},
    )
    return page("Receipts", body, base_path=BASE_PATH, identity=identity)


def _enrichment_html(item: dict[str, Any]) -> str:
    enrichment = item.get("enrichment") or {}
    if not enrichment:
        if item.get('recommendation'):
            return '<span class="muted">No accepted match yet</span>'
        return '<span class="muted">Not enriched</span>'
    provider = esc(enrichment.get("provider"))
    status = esc(enrichment.get("status"))
    confidence = enrichment.get("confidence")
    confidence_text = f"{float(confidence) * 100:.1f}%" if confidence is not None else ""
    query = esc(enrichment.get("search_query"))
    candidate = esc(product_name(enrichment.get("candidate_title"), enrichment.get("candidate_url")))
    candidate_url = str(enrichment.get("candidate_url") or "")
    product_link = (
        f'<a href="{html.escape(candidate_url, quote=True)}" target="_blank" rel="noopener">{candidate or "Verified product"}</a>'
        if urlparse(candidate_url).scheme in {'http', 'https'} and urlparse(candidate_url).netloc
        else candidate
    )
    details = (
        f"<strong>{provider}</strong> · {status} · {esc(confidence_text)}"
        + (f"<br>{product_link}" if product_link else "")
        + (f"<details><summary>Search query</summary><code>{query}</code></details>" if query else "")
    )
    raw_title = enrichment.get('candidate_title')
    if raw_title and raw_title != product_name(raw_title, enrichment.get('candidate_url')):
        details += f'<details><summary>Source webpage title</summary>{esc(raw_title)}</details>'
    return details


def _item_reading_html(item: dict[str, Any]) -> str:
    original = str(item.get('item_name') or '')
    rec = item.get('recommendation') or {}
    accepted = bool(rec.get('accepted_at'))
    correction = corrected_reading(original, rec.get('evidence')) if rec.get('eligible') or accepted else None
    enrichment = item.get('enrichment') or {}
    if correction is None and enrichment.get('status') == 'accepted':
        correction = corrected_reading(original, enrichment.get('reading_evidence'))
        if correction:
            accepted = True
    result = f'<span>{esc(original)}</span>'
    if correction:
        if accepted:
            result = f'<strong>{esc(correction)}</strong><br><small>Accepted interpretation</small>'
            result += f'<details><summary>Import history</summary><small>Text selected during import</small><br>{esc(original)}</details>'
        else:
            result += f'<br><small>Proposed interpretation</small><br><strong>{esc(correction)}</strong>'
    variants = rec.get('ocr_variants') or []
    if variants:
        result += f'<details><summary>Related OCR observations ({len(variants)} readings)</summary>'
        result += '<p class="muted">Row association unverified. These observations may include neighbouring or duplicate receipt items. OCR output line numbers are not Ledger item numbers.</p><ul>'
        for variant in variants:
            result += f'<li><details><summary><strong>{esc(variant["reading"])}</strong> · {len(variant["observations"])} observations</summary><ul>'
            text_groups = {}
            for source in variant['observations']:
                text_groups.setdefault(source.get('text') or '', []).append(source)
            for text, sources in text_groups.items():
                result += '<li>'
                if text and text != variant['reading']:
                    result += f'<code>{esc(text)}</code>'
                count = len(sources)
                result += f'<details><summary>Source records ({count})</summary><ul>'
                for source in sources:
                    kind = source.get('kind')
                    if kind == 'ocr_consensus':
                        provenance = 'Combined OCR text · OCR run UUID not recorded for this source'
                    elif kind == 'ocr_pass' or source.get('pass_id') is not None:
                        provenance = 'OCR pass'
                        if not source.get('ocr_run_uuid'):
                            provenance += ' · OCR run UUID not recorded for this source'
                    else:
                        provenance = 'OCR observation'
                    label = ' · '.join(f'{"OCR output line" if key == "line_number" else key.replace("_", " ")}: {source[key]}' for key in ('engine', 'pass_id', 'variant', 'ocr_run_uuid', 'page_number', 'line_number') if source.get(key) is not None)
                    result += f'<li><small>{esc(provenance)}</small><br><small>{esc(label)}</small>'
                    if source.get('id'):
                        result += f'<br><small>Observation ID: {esc(source["id"])}</small>'
                    if source.get('evidence_id') is not None:
                        result += f'<br><small>Receipt evidence ID: {esc(source["evidence_id"])}</small>'
                    result += '</li>'
                result += '</ul></details></li>'
            result += '</ul></details></li>'
        result += '</ul><p class="muted">An OCR run UUID is shared by the passes and lines from that run. Observation IDs distinguish the individual sources.</p></details>'
    return result


def _item_description(item: dict[str, Any]) -> str | None:
    description = item.get('product_description')
    for record in (item.get('enrichment') or {}, item.get('recommendation') or {}):
        if (description and description == record.get('candidate_title')
                and item.get('product_url') == record.get('candidate_url')):
            return product_name(description, item.get('product_url'))
    return description


def _recommendation_html(item: dict[str, Any], source_sha256: str) -> str:
    rec = item.get('recommendation')
    if not rec:
        return ''
    score = rec.get('confidence')
    confidence = f"{float(score)*100:.1f}%" if isinstance(score, (int, float)) else 'unknown'
    url = str(rec.get('candidate_url') or '')
    title = esc(product_name(rec.get('candidate_title'), url) or 'No product recommendation')
    if urlparse(url).scheme in {'http', 'https'} and urlparse(url).netloc:
        title = f'<a href="{esc(url)}" target="_blank" rel="noopener noreferrer">{title}</a>'
    state = 'Accepted recommendation' if rec.get('accepted_at') else 'Saved recommendation'
    result = f'<div class="card receipt-recommendation"><strong>{state}</strong><br>{title}<br>Evidence score: {esc(confidence)} · {esc(rec.get("disposition"))}'
    tokens = (rec.get('evidence') or {}).get('tokens') or []
    if tokens:
        result += '<br>' + esc(' · '.join(f"{t.get('token')} → {t.get('matched')} ({t.get('weight')})" for t in tokens))
    result += f'<details><summary>Recommendation evidence</summary><p>Run: {esc(rec["run_uuid"])}<br>Completed: {esc(rec.get("completed_at"))}</p>'
    result += f'<pre>{esc(json.dumps({"source_title":rec.get("candidate_title"), "evidence":rec.get("evidence"), "reasons":rec.get("reasons")}, indent=2))}</pre></details>'
    if rec.get('accepted_at'):
        result += f'<small>Accepted {esc(rec["accepted_at"])}</small>'
        if item.get('product_url') != rec.get('candidate_url') or item.get('product_description') not in {rec.get('candidate_title'), product_name(rec.get('candidate_title'), rec.get('candidate_url'))}:
            result += '<p class="muted">Acceptance recorded; the current description differs.</p>'
    elif rec.get('eligible'):
        values = {'expense_item_id': item['expense_item_id'], 'collaboration_id': rec['id'],
                  'expected_description': item.get('product_description'), 'expected_url': item.get('product_url')}
        result += f'<form class="accept-recommendation" data-receipt="{esc(source_sha256)}" data-request="{esc(json.dumps(values))}"><button type="submit">Accept recommendation for this item</button><p class="accept-status" role="status"></p></form>'
    else:
        result += '<p class="muted">Requires review; no eligible recommendation to accept.</p>'
    return result + '</div>'


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
    merchant = evidence.get('merchant') or expense.get('store_name')
    receipt_date = evidence.get('transaction_datetime') or expense.get('transaction_datetime') or expense.get('order_date')
    total = evidence.get('total')
    if total is None:
        total = expense.get('expense_total')
    title = receipt_label(expense_pk, merchant, receipt_date, total)

    canonical_expense = (
        f'<a href="{BASE_PATH}/api/expenses/{expense_pk}">Expense {esc(expense_pk)} (API)</a>'
        if expense_pk
        else '<span class="muted">Not created</span>'
    )
    header = f"""<div class="card"><div class="grid">
<div><strong>Receipt</strong><br>{esc(filename)}</div>
<div><strong>Merchant</strong><br>{esc(merchant)}</div>
<div><strong>Date/time</strong><br>{esc(receipt_date)}</div>
<div><strong>Total</strong><br>{money(total)}</div>
<div><strong>Processing</strong><br>{esc(receipt.get('processing_status'))}</div>
<div><strong>Extraction</strong><br>{esc(evidence.get('extraction_status') or expense.get('extraction_status'))}</div>
<div><strong>Source</strong><br>{esc(source_reference)}</div>
<div><strong>Accounting record</strong><br>{canonical_expense}</div>
</div></div>"""

    item_rows = []
    for item in expense.get("items", ()):
        description_html = esc(_item_description(dict(item))) or '<span class="muted">Awaiting product identification</span>'
        item_rows.append(
            "<tr>"
            f"<td>{_item_reading_html(dict(item))}</td>"
            f"<td>{description_html}</td>"
            f"<td>{_enrichment_html(dict(item))}{_recommendation_html(dict(item), source_sha256)}</td>"
            f"<td>{esc(item.get('budget_category'))}</td>"
            f"<td>{esc(item.get('category_group_name'))}</td>"
            f'<td class="num">{money(item.get("line_total"))}</td>'
            f"<td>{esc(item.get('category_source'))}</td>"
            "</tr>"
        )
    items_html = (
        '<h2>Line items</h2>'
        + (f'<form id="refresh-items" data-receipt="{esc(source_sha256)}"><button type="submit">Apply accepted matches and category rules</button>'
           '<p>Fills empty product fields from previously accepted matches and categorizes unresolved items. Existing descriptions and categories are preserved. No model or search calls.</p>'
           '<p id="refresh-status" role="status"></p></form>' if expense.get('items') else '')
        + '<table><thead><tr><th>Receipt item</th><th>Description</th>'
        "<th>Enrichment</th><th>Category</th><th>Group</th><th>Amount</th><th>Category source</th>"
        "</tr></thead><tbody>"
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
        f'<a href="{BASE_PATH}/dashboard">Accounting &amp; review dashboard</a> · '
        f'<a href="{receipt_graph_url(source_sha256)}">View receipt knowledge graph</a></p>'
        + header
        + review_html
        + '<style>.receipt-recommendation{min-width:18rem;max-width:26rem;overflow-wrap:anywhere}.receipt-recommendation pre{white-space:pre-wrap;overflow-wrap:anywhere}</style>'
        + items_html
        + """<script>
for(const form of document.querySelectorAll('.accept-recommendation')) {
  form.addEventListener('submit', async event => {
    event.preventDefault();const button=form.querySelector('button'),status=form.querySelector('.accept-status');
    button.disabled=true;status.textContent='Saving acceptance…';
    try {
      const response=await fetch(""" + json.dumps(BASE_PATH) + """+'/api/receipts/'+encodeURIComponent(form.dataset.receipt)+'/accept-recommendation', {
        method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Ledger-Action':'accept-product-recommendation'},body:form.dataset.request});
      const result=await response.json();if(!response.ok)throw Error(result.detail||'Unable to accept recommendation');
      status.textContent='Accepted. Refreshing receipt…';window.location.reload();
    } catch(error) {status.textContent=error.message;button.disabled=false;}
  });
}
const refresh=document.getElementById('refresh-items');
if(refresh) refresh.addEventListener('submit',async event=>{
  event.preventDefault();const button=refresh.querySelector('button'),status=document.getElementById('refresh-status');
  button.disabled=true;status.textContent='Applying accepted matches and category rules…';
  try{
    const response=await fetch(""" + json.dumps(BASE_PATH) + """+'/api/receipts/'+encodeURIComponent(refresh.dataset.receipt)+'/refresh-items',{
      method:'POST',credentials:'same-origin',headers:{'X-Ledger-Action':'refresh-receipt-items'}});
    const result=await response.json();if(!response.ok)throw Error(result.detail||'Unable to refresh items');
    sessionStorage.setItem('receipt-refresh:'+refresh.dataset.receipt,JSON.stringify(result));window.location.reload();
  }catch(error){status.textContent=error.message;button.disabled=false;}
});
if(refresh){const key='receipt-refresh:'+refresh.dataset.receipt,saved=sessionStorage.getItem(key);
  if(saved){sessionStorage.removeItem(key);const result=JSON.parse(saved);
    document.getElementById('refresh-status').textContent='Applied '+result.products_applied+' products and '+result.categories_applied+' categories. '+result.products_pending+' items still need product identification.';
  }
}
</script>"""
    )
    return page(title, body, base_path=BASE_PATH, identity=identity)


def main() -> None:
    import uvicorn

    uvicorn.run(
        "home_budget_pipeline.web.receipt_app:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8080")),
    )


if __name__ == "__main__":
    main()
