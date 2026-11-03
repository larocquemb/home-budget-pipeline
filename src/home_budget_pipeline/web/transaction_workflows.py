"""Bank transaction detail and human-confirmed allocations."""
from typing import Any
from fastapi import Depends, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .app import app, BASE_PATH, query_service, authenticated_identity
from .queries import LedgerQueryService
from .render import page, esc, money, table
from ..transactions.workflows import allocation_rows, receipt_candidates, save_allocations


class AllocationRequest(BaseModel):
    expected_version: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=2000)
    allocations: list[dict[str, Any]] = Field(min_length=1, max_length=100)


def detail(service, transaction_id):
    rows = service._fetch('SELECT * FROM budget.financial_transactions WHERE id=%s', (transaction_id,))
    if not rows:
        raise HTTPException(404, 'Transaction not found')
    with service._connect() as conn:
        with conn.cursor() as cur:
            allocations, version = allocation_rows(cur, transaction_id)
    return rows[0], allocations, version


@app.get(f'{BASE_PATH}/api/transactions/{{transaction_id}}')
def transaction_detail(transaction_id: int, service: LedgerQueryService = Depends(query_service),
                       _: dict = Depends(authenticated_identity)):
    tx, allocations, version = detail(service, transaction_id)
    return {'transaction': tx, 'allocations': allocations, 'version': version,
            'receipt_candidates': receipt_candidates(service, tx)}


@app.post(f'{BASE_PATH}/api/transactions/{{transaction_id}}/allocations')
def allocate_transaction(transaction_id: int, body: AllocationRequest,
                         x_ledger_action: str | None = Header(default=None),
                         service: LedgerQueryService = Depends(query_service),
                         identity: dict = Depends(authenticated_identity)):
    if x_ledger_action != 'transaction-allocation':
        raise HTTPException(403, 'Transaction allocation action header required')
    try:
        with service._connect() as conn:
            return save_allocations(conn, transaction_id, body.allocations, body.expected_version,
                                    identity.get('email') or identity['user'], body.reason, apply=True)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409 if 'changed' in str(exc) else 422, str(exc)) from exc


@app.get(f'{BASE_PATH}/transactions/{{transaction_id}}', response_class=HTMLResponse)
def transaction_page(transaction_id: int, service: LedgerQueryService = Depends(query_service),
                     identity: dict = Depends(authenticated_identity)):
    tx, allocations, version = detail(service, transaction_id)
    kind = (tx['raw_source'] or {}).get('kind') or 'unknown'
    body = f'''<p><a href="{BASE_PATH}/transactions">All transactions</a></p>
<div class="card"><strong>{esc(tx['description'])}</strong>
<p>Bank date: {esc(tx['transaction_date'])} · Amount: {money(tx['amount'])} {esc(tx['currency'])}
 · Card ending {esc(tx['card_last4'])} · {esc(kind)}</p>
<p class="muted">The bank export does not identify whether its date is a purchase or posting date.
Purchases are positive; credits and payments are negative.</p></div>'''
    if kind == 'payment':
        return page('Card payment', body + '<p>This is a transfer paying the card balance. It is excluded from expense allocations.</p>', base_path=BASE_PATH, identity=identity)
    suggestions = receipt_candidates(service, tx)
    body += '<h2>Receipt candidates</h2><p>Suggestions use equal amounts and dates within seven days. Check the original receipt, merchant and card before confirming. A bank description does not change the receipt extraction.</p>'
    body += table(suggestions, (('expense_pk','Expense'),('merchant','Merchant'),('store','Store'),
                                ('transaction_datetime','Receipt date'),('date_source','Date source'),('date_distance_days','Days apart'),
                                ('total','Total'),('extraction_status','Extraction')),
                  links={'expense_pk': f'{BASE_PATH}/expenses/{{value}}'}, money_columns={'total'})
    body += '<h2>Confirmed allocations</h2><p>Use one row for a whole purchase, or add rows for a split. Expense links are optional. Categories and people apply to these bank allocations and leave receipt line items intact.</p>'
    body += '<style>#allocation-form input{box-sizing:border-box;width:100%;min-width:0}#allocation-form table{table-layout:fixed}#allocation-form th:last-child{width:5rem}</style>'
    body += '<form id="allocation-form"><table><thead><tr><th>Amount</th><th>Category</th><th>Person</th><th>Expense ID (optional)</th><th>Note</th><th></th></tr></thead><tbody id="allocations">'
    rows = allocations or [{'amount': tx['amount'], 'category': '', 'person': '', 'expense_pk': None, 'note': ''}]
    for row in rows:
        body += allocation_html(row)
    body += '''</tbody></table><button type="button" id="add-allocation">Add split</button>
<p id="allocation-balance" role="status"></p>
<label>Reason for this allocation or correction <input name="reason" required maxlength="2000"></label>
<button type="submit">Save confirmed allocations</button><p id="allocation-status" role="status"></p></form>'''
    history = service._fetch('''SELECT version,actor,reason,recorded_at FROM budget.transaction_allocation_audit
        WHERE transaction_id=%s ORDER BY version DESC''', (transaction_id,))
    body += '<h2>Allocation history</h2>' + table(history, (('version','Version'),('actor','Recorded by'),('reason','Reason'),('recorded_at','Recorded at')))
    import json
    body += '<template id="allocation-template">' + allocation_html({}) + '</template>'
    body += '<script>\nconst allocationVersion=' + str(version) + ';\nconst bankCents=' + str(int(tx['amount']*100)) + ';\nconst allocationUrl=' + json.dumps(f'{BASE_PATH}/api/transactions/{transaction_id}/allocations') + ';\n' + SCRIPT + '</script>'
    return page('Bank transaction '+str(transaction_id), body, base_path=BASE_PATH, identity=identity)


def allocation_html(row):
    fields = [('amount','number','step="0.01" required'),('category','text','required maxlength="200"'),
              ('person','text','maxlength="2000"'),('expense_pk','number','min="1" step="1"'),('note','text','maxlength="2000"')]
    return '<tr>' + ''.join(f'<td><input aria-label="{esc(k)}" data-field="{k}" type="{t}" {attrs} value="{esc(row.get(k))}"></td>' for k,t,attrs in fields) + '<td><button type="button" data-remove>Remove</button></td></tr>'


SCRIPT = r'''
const form=document.getElementById('allocation-form'), rows=document.getElementById('allocations');
function balance(){
  const cents=[...rows.querySelectorAll('[data-field="amount"]')].reduce((s,e)=>s+Math.round(Number(e.value)*100),0);
  document.getElementById('allocation-balance').textContent='Allocated: '+(cents/100).toFixed(2)+' · Remaining: '+((bankCents-cents)/100).toFixed(2);
}
document.getElementById('add-allocation').onclick=()=>{rows.append(document.getElementById('allocation-template').content.cloneNode(true));balance();};
rows.onclick=e=>{if(e.target.hasAttribute('data-remove')){e.target.closest('tr').remove();balance();}};
rows.oninput=balance;balance();
form.onsubmit=async e=>{
  e.preventDefault();const status=document.getElementById('allocation-status'),button=form.querySelector('[type="submit"]');button.disabled=true;
  try{
    const allocations=[...rows.querySelectorAll('tr')].map(row=>Object.fromEntries([...row.querySelectorAll('[data-field]')].map(el=>[el.dataset.field,el.dataset.field==='expense_pk'?(el.value?Number(el.value):null):el.value])));
    const response=await fetch(allocationUrl,{method:'POST',headers:{'Content-Type':'application/json','X-Ledger-Action':'transaction-allocation'},body:JSON.stringify({expected_version:allocationVersion,reason:form.reason.value,allocations})});
    const result=await response.json();if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:'Check the allocation fields.');
    window.location.reload();
  }catch(error){status.textContent=error.message;button.disabled=false;}
};
'''
