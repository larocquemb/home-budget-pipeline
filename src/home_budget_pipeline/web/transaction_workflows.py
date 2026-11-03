"""Bank transaction detail and human-confirmed allocations."""
from typing import Any
from fastapi import Depends, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .app import app, BASE_PATH, query_service, authenticated_identity
from .queries import LedgerQueryService
from .render import page, esc, money, table
from ..transactions.workflows import allocation_rows, receipt_candidates, save_allocations
from ..transactions.database import service as accounting_service


@app.post(f'{BASE_PATH}/api/transaction-imports/refresh')
def refresh_imports(x_ledger_action:str|None=Header(default=None),
                    identity:dict=Depends(authenticated_identity)):
    if x_ledger_action!='transaction-import':
        raise HTTPException(403,'Transaction import action header required')
    from ..transactions.discovery import scan,source_root
    try:
        with accounting_service()._connect() as conn:
            return scan(conn,source_root(),identity.get('email') or identity['user'],immediate=True,retry_failed=True)
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from exc


def import_control():
    return '''<button type="button" id="refresh-imports">Import / refresh transactions</button>
<span id="import-result" role="status"></span><script>
document.getElementById('refresh-imports').onclick=async function(){
 const status=document.getElementById('import-result');this.disabled=true;status.textContent='Importing downloads…';
 try{const response=await fetch('''+repr(BASE_PATH+'/api/transaction-imports/refresh')+''',{
  method:'POST',headers:{'X-Ledger-Action':'transaction-import'}});
  const result=await response.json();if(!response.ok)throw new Error(result.detail||'Import failed');
  if(result.busy){status.textContent='An import is already running. Check import status.';return;}
  status.textContent='Imported '+result.new_transactions+' new transactions; '+result.unchanged+' unchanged files; '+result.failed+' failed; '+result.waiting+' still writing.';
  const link=document.createElement('a');link.href=window.location.href;link.textContent=' Refresh view';status.append(link);
 }catch(error){status.textContent=error.message;}finally{this.disabled=false;}
};</script>'''


class AllocationRequest(BaseModel):
    expected_version: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=2000)
    allocations: list[dict[str, Any]] = Field(min_length=1, max_length=100)


def detail(service, transaction_id):
    bank=accounting_service()
    rows = bank._fetch('SELECT * FROM accounting.financial_transactions WHERE id=%s', (transaction_id,))
    if not rows:
        raise HTTPException(404, 'Transaction not found')
    with bank._connect() as conn:
        with conn.cursor() as cur:
            allocations, version = allocation_rows(cur, transaction_id)
    # Expense integers can be reused after a receipt rebuild. Resolve saved links
    # by immutable source hash; never send a recycled integer back as identity.
    for allocation in allocations:
        if allocation['receipt_source_sha256']:
            current=service._fetch('SELECT expense_pk FROM budget.receipt_evidence WHERE source_sha256=%s',
                                   (allocation['receipt_source_sha256'],))
            allocation['expense_pk']=current[0]['expense_pk'] if current else None
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
        with accounting_service()._connect() as conn:
            return save_allocations(conn, transaction_id, body.allocations, body.expected_version,
                                    identity.get('email') or identity['user'], body.reason, apply=True,receipt_service=service)
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
    if tx['currency']!='CAD':
        body+='<p>Receipt matching is unavailable until the receipt currency can be verified. Allocations remain in the original account currency.</p>'
    body += table(suggestions, (('expense_pk','Expense'),('merchant','Merchant'),('store','Store'),
                                ('transaction_datetime','Receipt date'),('date_source','Date source'),('date_distance_days','Days apart'),
                                ('total','Total'),('extraction_status','Extraction')),
                  links={'expense_pk': f'{BASE_PATH}/expenses/{{value}}'}, money_columns={'total'})
    body += '<h2>Confirmed allocations</h2><p>Use one row for a whole purchase, or add rows for a split. Expense links are optional. Categories and people apply to these bank allocations and leave receipt line items intact.</p>'
    body += '<style>#allocation-form input{box-sizing:border-box;width:100%;min-width:0}#allocation-form table{table-layout:fixed}#allocation-form th:last-child{width:5rem}</style>'
    body += '<form id="allocation-form"><table><thead><tr><th>Amount</th><th>Category</th><th>Person</th><th>Expense ID (optional)</th><th>Note</th><th></th></tr></thead><tbody id="allocations">'
    rows = allocations or [{'amount': tx['amount'], 'category': '', 'person': '', 'expense_pk': None, 'note': ''}]
    for row in rows:
        body += allocation_html(row, suggestions)
    body += '''</tbody></table><button type="button" id="add-allocation">Add split</button>
<p id="allocation-balance" role="status"></p>
<label>Reason for this allocation or correction <input name="reason" required maxlength="2000"></label>
<button type="submit">Save confirmed allocations</button><p id="allocation-status" role="status"></p></form>'''
    history = accounting_service()._fetch('''SELECT version,actor,reason,recorded_at FROM accounting.transaction_allocation_audit
        WHERE transaction_id=%s ORDER BY version DESC''', (transaction_id,))
    body += '<h2>Allocation history</h2>' + table(history, (('version','Version'),('actor','Recorded by'),('reason','Reason'),('recorded_at','Recorded at')))
    import json
    body += '<template id="allocation-template">' + allocation_html({}, suggestions) + '</template>'
    body += '<script>\nconst allocationVersion=' + str(version) + ';\nconst bankCents=' + str(int(tx['amount']*100)) + ';\nconst allocationUrl=' + json.dumps(f'{BASE_PATH}/api/transactions/{transaction_id}/allocations') + ';\n' + SCRIPT + '</script>'
    return page('Bank transaction '+str(transaction_id), body, base_path=BASE_PATH, identity=identity)


def allocation_html(row, suggestions):
    fields = [('amount','number','step="0.01" required'),('category','text','required maxlength="200"'),
              ('person','text','maxlength="2000"'),('expense_pk','number','min="1" step="1"'),('note','text','maxlength="2000"')]
    sha=row.get('receipt_source_sha256') or ''
    link=(f'<small><a href="{BASE_PATH}/receipts/{esc(sha)}">Saved receipt source</a></small>' if sha else '')
    options = ''.join(f'<option value="{esc(r["expense_pk"])}" data-receipt-source="{esc(r.get("receipt_source_sha256"))}">{esc(r["merchant"])} · {esc(r["transaction_datetime"])} · {money(r["total"])} · Expense {esc(r["expense_pk"])}</option>' for r in suggestions)
    selection = ('<label>Choose receipt <select data-receipt-choice><option value="">Select a candidate…</option>' + options + '</select></label>') if suggestions else ''
    return '<tr>' + ''.join(f'<td><input aria-label="{esc(k)}" data-field="{k}" type="{t}" {attrs} value="{esc(row.get(k))}"></td>' for k,t,attrs in fields) + f'<td><input type="hidden" data-field="receipt_source_sha256" value="{esc(sha)}">{link}{selection}<button type="button" data-remove>Remove</button></td></tr>'


SCRIPT = r'''
const form=document.getElementById('allocation-form'), rows=document.getElementById('allocations');
function balance(){
  const cents=[...rows.querySelectorAll('[data-field="amount"]')].reduce((s,e)=>s+Math.round(Number(e.value)*100),0);
  document.getElementById('allocation-balance').textContent='Allocated: '+(cents/100).toFixed(2)+' · Remaining: '+((bankCents-cents)/100).toFixed(2);
}
document.getElementById('add-allocation').onclick=()=>{rows.append(document.getElementById('allocation-template').content.cloneNode(true));balance();};
rows.onclick=e=>{if(e.target.hasAttribute('data-remove')){e.target.closest('tr').remove();balance();}};
rows.onchange=e=>{
  if(e.target.hasAttribute('data-receipt-choice') && e.target.value){
    const row=e.target.closest('tr');
    row.querySelector('[data-field="expense_pk"]').value=e.target.value;
    row.querySelector('[data-field="receipt_source_sha256"]').value=e.target.selectedOptions[0].dataset.receiptSource || '';
  }
};
rows.oninput=e=>{
  if(e.target.dataset.field==='expense_pk')e.target.closest('tr').querySelector('[data-field="receipt_source_sha256"]').value='';
  balance();
};balance();
form.onsubmit=async e=>{
  e.preventDefault();const status=document.getElementById('allocation-status'),button=form.querySelector('[type="submit"]');button.disabled=true;
  try{
    const allocations=[...rows.querySelectorAll('tr')].map(row=>Object.fromEntries([...row.querySelectorAll('[data-field]')].map(el=>[el.dataset.field,el.dataset.field==='expense_pk'?(el.value?Number(el.value):null):el.dataset.field==='receipt_source_sha256'?(el.value||null):el.value])));
    const response=await fetch(allocationUrl,{method:'POST',headers:{'Content-Type':'application/json','X-Ledger-Action':'transaction-allocation'},body:JSON.stringify({expected_version:allocationVersion,reason:form.reason.value,allocations})});
    const result=await response.json();if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:'Check the allocation fields.');
    window.location.reload();
  }catch(error){status.textContent=error.message;button.disabled=false;}
};
'''


@app.get(f'{BASE_PATH}/accounts',response_class=HTMLResponse)
def accounts_page(identity:dict=Depends(authenticated_identity)):
    from ..transactions.accounts import account_rows
    rows=account_rows(accounting_service())
    body='<p>Accounts use last-four identifiers. Shared card accounts use the primary card ending. Full account and card numbers are not stored. Ownership assertions are separate from historical payer attribution.</p>'
    body+=table(rows,(('nickname','Nickname'),('account_reference','Account'),('institution','Institution'),('account_type','Type'),('currency','Currency'),('primary_card_last4','Primary card ending'),
                     ('last4','Account / card ending'),('owner_name','Owner / cardholder'),('ownership_role','Role'),
                     ('effective_from','Effective from'),('effective_to','Effective to'),('asserted_at','Recorded at'),('id','Transactions')),
                links={'id':f'{BASE_PATH}/cad-ledger?account_id={{value}}'},link_labels={'id':'View transactions'})
    body+='<p>Blank effective dates mean historical ownership dates have not been supplied.</p>'
    return page('Accounts and ownership',body,base_path=BASE_PATH,identity=identity)


@app.get(f'{BASE_PATH}/api/accounts')
def accounts_api(_:dict=Depends(authenticated_identity)):
    from ..transactions.accounts import account_rows
    return account_rows(accounting_service())


@app.get(f'{BASE_PATH}/transaction-imports',response_class=HTMLResponse)
def transaction_imports_page(identity:dict=Depends(authenticated_identity)):
    from ..transactions.discovery import status
    from pathlib import Path
    with accounting_service()._connect() as conn:
        rows=status(conn)
    for row in rows:
        row['file']=str(Path(row['source_uri']).relative_to(row['root_uri']))
    body=import_control()+'<p>Refresh after downloading exports. Failed files retain their errors for the next refresh.</p>'
    body+=table(rows,(('file','Download'),('status','Status'),('checked_at','Last checked'),
                     ('imported_at','Imported'),('error_summary','Issue')))
    return page('Transaction imports',body,base_path=BASE_PATH,identity=identity)


@app.post(f'{BASE_PATH}/api/transactions/fx-update')
def refresh_fx(x_ledger_action:str|None=Header(default=None),identity:dict=Depends(authenticated_identity)):
    if x_ledger_action!='fx-update':
        raise HTTPException(403,'FX update action header required')
    from ..transactions.ledger import update_rates
    try:
        with accounting_service()._connect() as conn:
            return update_rates(conn,identity.get('email') or identity['user'])
    except (ValueError,OSError) as exc:
        raise HTTPException(502,'Rate update failed; existing rates are unchanged. '+type(exc).__name__) from exc


@app.get(f'{BASE_PATH}/cad-ledger',response_class=HTMLResponse)
def cad_ledger_page(limit:int=100,offset:int=0,account:str='',from_date:str='',to_date:str='',direction:str='desc',account_id:int|None=None,
                    identity:dict=Depends(authenticated_identity)):
    from datetime import date
    from ..transactions.ledger import ledger_rows
    from .render import pager
    try:
        result=ledger_rows(accounting_service(),limit=limit,offset=offset,account=account,direction=direction,account_id=account_id,
            start=date.fromisoformat(from_date) if from_date else None,end=date.fromisoformat(to_date) if to_date else None)
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from exc
    body=import_control()+''' <button type="button" id="refresh-fx">Update CAD rates</button><span id="fx-result" role="status"></span>
<p>Original amounts are retained. Positive amounts are charges/withdrawals; negative amounts are payments/deposits.
CAD equivalents use <a href="https://www.bankofcanada.ca/rates/exchange/daily-exchange-rates/">Bank of Canada daily reference rates</a>,
using the latest available business-day rate on or before the bank date (up to seven days).
These are reporting equivalents, not bank settlement costs. Transfers and card payments remain visible.</p>'''
    account_filter = f'<input type="hidden" name="account_id" value="{account_id}">' if account_id is not None else ''
    if account_id is not None:
        body += f'<p>Showing this account only. <a href="{BASE_PATH}/accounts">Choose another account</a> · <a href="{BASE_PATH}/cad-ledger">All accounts</a></p>'
    body+=f'''<form method="get">{account_filter}<label>Account or ending <input name="account" value="{esc(account)}"></label>
<label>From <input type="date" name="from_date" value="{esc(from_date)}"></label>
<label>To <input type="date" name="to_date" value="{esc(to_date)}"></label>
<label>Order <select name="direction"><option value="desc" {'selected' if direction=='desc' else ''}>Newest first</option>
<option value="asc" {'selected' if direction=='asc' else ''}>Oldest first</option></select></label><button>Filter</button></form>
<p>{result['total']} transactions · {result['missing_rates']} missing CAD rates.</p>'''
    rows=[]
    for row in result['rows']:
        rows.append({**row,'conversion':row['conversion_status'].replace('_',' '),
                     'fx_rate':f"{row['fx_rate']:.4f}" if row['fx_rate'] is not None else ''})
    body+=table(rows,(('transaction_id','Transaction'),('bank_date','Bank date'),('account','Account'),('account_last4','Ending'),('description','Description'),
        ('original_amount','Original amount'),('currency','Currency'),('amount_cad','CAD equivalent'),
        ('fx_rate','CAD per unit'),('rate_date','Rate date'),('conversion','Conversion')),
        links={'transaction_id':f'{BASE_PATH}/transactions/{{value}}'},money_columns={'original_amount','amount_cad'})
    body+=pager(BASE_PATH+'/cad-ledger',limit=limit,offset=offset,row_count=len(rows),total_count=result['total'],
                query={'account':account,'from_date':from_date,'to_date':to_date,'direction':direction,'account_id':account_id})
    body+='''<script>document.getElementById('refresh-fx').onclick=async function(){
 const status=document.getElementById('fx-result');this.disabled=true;status.textContent='Updating reference rates…';
 try{const response=await fetch('''+repr(BASE_PATH+'/api/transactions/fx-update')+''',{method:'POST',headers:{'X-Ledger-Action':'fx-update'}});
 const result=await response.json();if(!response.ok)throw new Error(result.detail||'Rate update failed');
 window.location.reload();}catch(error){status.textContent=error.message;this.disabled=false;}};</script>'''
    return page('CAD transaction ledger',body,base_path=BASE_PATH,identity=identity)
