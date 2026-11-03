"""Show current receipt categories without duplicating or prorating receipt expenses."""
from .app import BASE_PATH
from .render import table, esc, money


def render(service, candidates, allocations):
    receipts = {r['expense_pk'] for r in (*candidates, *allocations) if r.get('expense_pk')}
    if not receipts:
        return ''
    rows = service._fetch('''
        SELECT expense_pk,COALESCE(budget_category,'Uncategorized') AS category,
               sum(line_total) AS item_amount,
               count(*) FILTER (WHERE line_total IS NULL) AS missing_amounts
          FROM budget.expense_items WHERE expense_pk=ANY(%s)
         GROUP BY expense_pk,COALESCE(budget_category,'Uncategorized')
         ORDER BY expense_pk,category
    ''', (sorted(receipts),))
    confirmed = {r['expense_pk'] for r in allocations if r.get('expense_pk')}
    rows = [{**r, 'relationship': 'Confirmed link' if r['expense_pk'] in confirmed else 'Candidate'} for r in rows]
    summary = ('<h2>Receipt line-item categories</h2>'
        '<p>Current categories from the linked and candidate receipts. Candidates are unconfirmed. '
        'Amounts cover the whole receipt\'s line items, not a share of a split payment. '
        'Tax, fees and any difference from the bank amount are not distributed among categories. '
        'Uncategorized items and missing amounts need review on the receipt.</p>'
        + table(rows, (('expense_pk','Receipt'),('relationship','Relationship'),('category','Category'),
                       ('item_amount','Line-item total'),('missing_amounts','Items missing amounts')),
                links={'expense_pk':f'{BASE_PATH}/expenses/{{value}}'},money_columns={'item_amount'}))

    details = ''
    for expense_pk in sorted(confirmed):
        expense = service.expense_detail(expense_pk)
        if not expense:
            continue
        items = [{**item, 'description': item.get('product_description') or item.get('item_name'),
                  'budget_category': item.get('budget_category') or 'Uncategorized'}
                 for item in expense.get('items', ())]
        details += (f'<section class="linked-receipt-items"><h2><a href="{BASE_PATH}/expenses/{expense_pk}">Receipt {expense_pk}</a> — line items</h2>'
                    f'<p>{esc(expense.get("store_name"))} · Receipt total: {money(expense.get("expense_total"))} · {len(items)} items. '
                    'These are the current receipt items; edit descriptions and categories on the receipt.</p>'
                    + table(items, (('item_name','Receipt item'),('description','Description'),
                                    ('budget_category','Category'),('line_total','Amount')),
                            money_columns={'line_total'}) + '</section>')
    return details + summary
