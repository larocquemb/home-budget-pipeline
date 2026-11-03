"""Accounting connections fail closed instead of falling back to receipt storage."""
import os
from ..web.queries import LedgerQueryService


def connect():
    import psycopg
    from psycopg.rows import dict_row
    dsn = os.getenv('ACCOUNTING_DATABASE_URL')
    if not dsn:
        raise ValueError('ACCOUNTING_DATABASE_URL is required; bank records must not use the receipt database')
    from urllib.parse import urlsplit
    receipt=urlsplit(os.getenv('DATABASE_URL',''))
    bank=urlsplit(dsn)
    if (receipt.hostname,receipt.port,receipt.path)==(bank.hostname,bank.port,bank.path):
        raise ValueError('Accounting and receipts must use separate databases')
    return psycopg.connect(dsn, row_factory=dict_row, connect_timeout=10)


def service():
    return LedgerQueryService(connect)
