"""Receipt allocation review controls, kept separate from product/category acceptance."""
from .render import esc, money


def allocation_html(state, sha):
    snapshot, check = state['snapshot'], state['assessment']
    expense = snapshot['expense']
    label = 'Reconciled' if check['reconciled'] else 'Needs allocation review'
    result = '<section class="card"><h2>Item allocation: ' + label + '</h2>'
    result += (f'<p>Items: {money(check["item_total"])} · Allocation subtotal: {money(check["subtotal"])}'
               f' · Item difference: {money(check["item_difference"])}'
               f' · Receipt difference: {money(check["header_difference"])}</p>')
    if check['reasons']:
        result += '<p role="status">' + esc(', '.join(check['reasons']).replace('_', ' ')) + '</p>'
    result += ('<details><summary>Correct allocations against the original receipt</summary>'
               '<p>Use the original image. Enter each purchase once, including its allocated discounts or fees. '
               'Leave out unit-price, tender, loyalty and savings-summary rows. '
               'The receipt total and identity remain unchanged. Categories are reviewed separately.</p>')
    result += f'<form id="allocation-correction" data-receipt="{esc(sha)}" data-version="{esc(state["version"])}">'
    values = [('merchant', 'Merchant', expense.get('store_name') or '', 'text'),
              ('subtotal', 'Allocation subtotal', expense.get('receipt_item_subtotal'), 'number'),
              ('gst', 'Additional GST/HST', expense.get('receipt_gst') or 0, 'number'),
              ('pst', 'Additional PST', expense.get('receipt_pst') or 0, 'number'),
              ('included_tax', 'Tax already included (informational)', (state.get('correction') or {}).get('included_tax', 0), 'number')]
    for key, label, value, kind in values:
        extra = ' step="0.01"' if kind == 'number' else ' maxlength="300"'
        result += f'<label>{esc(label)} <input name="{key}" type="{kind}" value="{esc(value if value is not None else "")}"{extra} required></label> '
    result += f'<p>Unchanged receipt total: <strong>{money(expense.get("expense_total"))}</strong>. Included tax is recorded without adding it again.</p>'
    result += '<table><thead><tr><th>Purchase</th><th>Allocated amount</th><th>Remove</th></tr></thead><tbody id="allocation-rows">'
    for item in snapshot['items']:
        result += (f'<tr data-item="{int(item["id"])}"><td><input class="allocation-name" maxlength="1000" value="{esc(item["item_name"])}" required></td>'
                   f'<td><input class="allocation-amount" type="number" step="0.01" value="{esc(item["line_total"])}" required></td>'
                   '<td><input class="allocation-remove" type="checkbox" aria-label="Remove purchase from allocation"></td></tr>')
    result += ('</tbody></table><button id="add-allocation-item" type="button">Add purchase</button>'
               '<label>Correction reason <textarea name="reason" maxlength="2000" required></textarea></label>'
               '<button type="submit">Save verified allocations</button><p id="allocation-status" role="status"></p>'
               '</form></details></section>')
    return result
