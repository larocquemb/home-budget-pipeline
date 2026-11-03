"""Receipt navigation to confirmed bank allocations in separate accounting storage."""
import psycopg

from .app import BASE_PATH
from .render import table


def render(service, source_sha256):
    heading = '<section><h2>Linked bank transactions</h2>'
    try:
        rows = service.receipt_bank_links(source_sha256)
    except (ValueError, psycopg.Error):
        return heading + '<p>Bank links are unavailable. Try again when accounting is connected.</p></section>'
    if not rows:
        return heading + f'<p>No confirmed bank links. <a href="{BASE_PATH}/transactions">Open transactions</a> to review candidates and save a receipt allocation.</p></section>'
    return heading + table(rows, (
        ('transaction_id', 'Transaction'), ('transaction_date', 'Bank date'),
        ('account', 'Account'), ('description', 'Description'),
        ('bank_amount', 'Bank amount'), ('currency', 'Currency'), ('linked_amount', 'Linked amount')),
        links={'transaction_id': f'{BASE_PATH}/transactions/{{value}}'},
        money_columns={'bank_amount', 'linked_amount'}) + '</section>'
