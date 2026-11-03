"""Audited bank imports, read-only receipt suggestions and balanced allocations."""
from collections import Counter
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from pathlib import Path

from .cibc import parse_cibc


def ensure_schema(conn):
    from ..db_setup import SQL_DIR
    conn.execute((SQL_DIR / 'accounting.sql').read_text())


def import_cibc(conn, path, account_reference, actor, *, apply=False,account_type='credit_card',account_last4=None,extra_known=None,currency='CAD'):
    return import_bank_csv(conn,path,account_reference,actor,apply=apply,account_type=account_type,
                           account_last4=account_last4,extra_known=extra_known,currency=currency,institution='CIBC')


def import_bank_csv(conn,path,account_reference,actor,*,apply=False,account_type='credit_card',
                    account_last4=None,extra_known=None,currency='CAD',institution='CIBC',source_data=None):
    from psycopg.types.json import Jsonb
    data = Path(path).read_bytes() if source_data is None else source_data
    from .acu import parse_acu
    from .cibcus import parse_cibcus
    parsers={'CIBC':parse_cibc,'ACU':parse_acu,'CIBC US':parse_cibcus}
    if institution not in parsers:
        raise ValueError('Unsupported institution')
    rows = parsers[institution](data,account_reference,account_type=account_type,account_last4=account_last4)
    format_name=('cibc-card-v1' if account_type=='credit_card' else 'cibc-deposit-v1') if institution=='CIBC' else ('acu-deposit-v1' if institution=='ACU' else 'cibcus-deposit-v1')
    if currency not in {'CAD','USD'}:
        raise ValueError('Supported export currencies are CAD and USD')
    if currency!='CAD':
        from dataclasses import replace
        # Preserve historical CAD fingerprints while separating foreign currency.
        rows=[replace(r,fingerprint=hashlib.sha256((r.fingerprint+':'+currency).encode()).hexdigest()) for r in rows]
    digest = hashlib.sha256(data).hexdigest()
    if not actor.strip():
        raise ValueError('An actor is required')
    # All data/schema changes are in one transaction; previews only read.
    with conn.transaction():
        if apply:
            ensure_schema(conn)
            conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (institution.casefold()+'-import:' + account_reference.casefold(),))
        with conn.cursor() as cur:
            cur.execute('SELECT account_type,currency,institution FROM accounting.accounts WHERE account_reference=%s',(account_reference,))
            registered=cur.fetchone()
            if registered and (registered['account_type']!=account_type or registered['currency']!=currency or registered['institution']!=institution):
                raise ValueError('Account type or currency conflicts with the registered account')
            cur.execute('SELECT import_fingerprint FROM accounting.financial_transactions WHERE import_fingerprint=ANY(%s)',
                        ([r.fingerprint for r in rows],))
            existing = {r['import_fingerprint'] for r in cur.fetchall()}
            existing.update(extra_known or ())
            report = {'rows': len(rows), 'new': sum(r.fingerprint not in existing for r in rows),
                      'already_imported': sum(r.fingerprint in existing for r in rows),
                      'date_from': min(r.date for r in rows).isoformat(),
                      'date_to': max(r.date for r in rows).isoformat(),
                      'debits': str(sum((r.amount for r in rows if r.amount > 0), Decimal(0))),
                      'credits': str(-sum((r.amount for r in rows if r.amount < 0), Decimal(0))),
                      'kinds': dict(Counter(r.kind for r in rows)), 'source_sha256': digest, 'applied': apply,'currency':currency}
            if extra_known is not None:
                extra_known.update(r.fingerprint for r in rows)
            if not apply:
                return report
            cur.execute('''INSERT INTO accounting.accounts (account_reference,institution,account_type,primary_card_last4,currency)
                VALUES (%s,%s,%s,%s,%s) ON CONFLICT (account_reference) DO NOTHING''',
                        (account_reference,institution,account_type,account_last4 if account_type=='credit_card' else None,currency))
            cur.execute('SELECT id,account_type,currency FROM accounting.accounts WHERE account_reference=%s',(account_reference,))
            account=cur.fetchone();account_id=account['id']
            if account['account_type']!=account_type:
                raise ValueError('Existing account has a different account type')
            if account['currency']!=currency:
                raise ValueError('Existing account has a different currency; no conversion is inferred')
            if account_type!='credit_card':
                cur.execute('''INSERT INTO accounting.account_numbers(account_id,number_kind,last4)
                    VALUES(%s,'bank_account',%s) ON CONFLICT DO NOTHING''',(account_id,account_last4))
            cur.execute('''INSERT INTO accounting.transaction_imports
                (source_sha256,source_name,account_reference,format,row_count,actor)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (source_sha256,account_reference) DO UPDATE
                    SET source_sha256=EXCLUDED.source_sha256 RETURNING id''',
                        (digest, Path(path).name, account_reference, format_name,len(rows), actor))
            import_id = cur.fetchone()['id']
            cur.execute('''INSERT INTO accounting.source_artifacts(import_id,source_uri,source_sha256)
                VALUES (%s,%s,%s) ON CONFLICT(import_id,source_uri) DO NOTHING''',
                        (import_id,str(Path(path).resolve()),digest))
            for row in rows:
                raw = {'format': format_name, 'row': row.raw, 'kind': row.kind,
                       'export_date_semantics': 'bank_export_date; posting_vs_purchase_unspecified',
                       'source_sha256': digest, 'source_row': row.number}
                if institution=='ACU':
                    raw['redacted_columns']=['Unique ID']
                elif institution=='CIBC US':
                    raw['redacted_columns']=['numeric identifiers of eight or more digits in descriptions']
                elif institution=='CIBC':
                    raw['redacted_columns']=['numeric identifiers of eight or more digits in Description']
                cur.execute('''INSERT INTO accounting.financial_transactions
                    (institution,account_id,source_account_reference,transaction_date,amount,currency,
                     description,merchant_text,card_last4,import_fingerprint,raw_source)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (import_fingerprint) DO NOTHING''',
                            (institution,account_id, account_reference, row.date, row.amount, currency,row.description, row.description,
                             row.card_last4, row.fingerprint, Jsonb(raw)))
                if row.card_last4:
                    cur.execute('''INSERT INTO accounting.account_numbers (account_id,number_kind,last4)
                        VALUES (%s,'card',%s) ON CONFLICT (account_id,number_kind,last4) DO NOTHING''',
                                (account_id,row.card_last4))
                cur.execute('''INSERT INTO accounting.transaction_import_rows
                    (import_id,row_number,transaction_id,raw_row)
                    SELECT %s,%s,id,%s FROM accounting.financial_transactions WHERE import_fingerprint=%s
                    ON CONFLICT (import_id,row_number) DO NOTHING''',
                            (import_id, row.number, Jsonb(row.raw), row.fingerprint))
            report['import_id'] = import_id
            return report


def allocation_rows(cur, transaction_id):
    cur.execute('''SELECT allocation_number,amount,category,person,expense_pk,note,receipt_source_sha256
        FROM accounting.transaction_allocations WHERE transaction_id=%s ORDER BY allocation_number''', (transaction_id,))
    rows = cur.fetchall()
    cur.execute('SELECT COALESCE(MAX(version),0) AS version FROM accounting.transaction_allocation_audit WHERE transaction_id=%s', (transaction_id,))
    return rows, cur.fetchone()['version']


def normalize_allocations(rows, total):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise ValueError('Supply between 1 and 100 allocations')
    result = []
    for number, row in enumerate(rows, 1):
        if not isinstance(row, dict) or set(row) - {'amount','category','person','expense_pk','note','receipt_source_sha256'}:
            raise ValueError('Unsupported allocation fields')
        try:
            amount = Decimal(str(row.get('amount', '')))
            if not amount.is_finite() or amount == 0 or amount != amount.quantize(Decimal('.01')):
                raise ValueError()
        except (ValueError, InvalidOperation):
            raise ValueError('Allocation amounts must be nonzero amounts in cents') from None
        if (amount > 0) != (total > 0):
            raise ValueError('Allocation signs must agree with the bank amount')
        category = row.get('category')
        if not isinstance(category, str) or not category.strip() or len(category) > 200:
            raise ValueError('Each allocation needs a category (at most 200 characters)')
        expense_pk = row.get('expense_pk')
        if expense_pk is not None and (type(expense_pk) is not int or expense_pk <= 0):
            raise ValueError('Expense must be a positive integer or null')
        for key in ('person', 'note'):
            if row.get(key) is not None and (not isinstance(row[key], str) or len(row[key]) > 2000):
                raise ValueError(f'Invalid {key}')
        sha=row.get('receipt_source_sha256')
        if sha is not None and (not isinstance(sha,str) or not re.fullmatch('[0-9a-f]{64}',sha)):
            raise ValueError('Invalid receipt source identity')
        result.append({'allocation_number': number, 'amount': amount, 'category': category.strip(),
                       'person': (row.get('person') or '').strip() or None,
                       'expense_pk': expense_pk, 'note': (row.get('note') or '').strip(), 'receipt_source_sha256':sha})
    if sum(r['amount'] for r in result) != total:
        raise ValueError('Allocations must sum exactly to the bank transaction amount')
    return result


def save_allocations(conn, transaction_id, rows, expected_version, actor, reason, *, apply=False, receipt_service=None):
    from psycopg.types.json import Jsonb
    if type(expected_version) is not int or expected_version < 0:
        raise ValueError('An expected allocation version is required')
    if not actor.strip() or not reason.strip() or len(reason) > 2000:
        raise ValueError('An actor and reason (at most 2000 characters) are required')
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute('SELECT id,amount,currency,raw_source FROM accounting.financial_transactions WHERE id=%s FOR UPDATE', (transaction_id,))
            tx = cur.fetchone()
            if tx is None:
                raise LookupError('Transaction not found')
            if (tx['raw_source'] or {}).get('kind') == 'payment':
                raise ValueError('Card payments are transfers, not expense allocations')
            before, version = allocation_rows(cur, transaction_id)
            if version != expected_version:
                raise ValueError('Allocations changed; reload the transaction before saving')
            after = normalize_allocations(rows, tx['amount'])
            if tx['currency']!='CAD' and any(r['expense_pk'] or r['receipt_source_sha256'] for r in after):
                raise ValueError('Receipt currency is not verified for this account; record allocations without a receipt link')
            for r in after:
                if r['expense_pk']:
                    if receipt_service is None:
                        raise ValueError('A receipt connection is required to confirm an expense link')
                    evidence=receipt_service._fetch('''SELECT re.source_sha256 FROM budget.receipt_evidence re
                        WHERE re.expense_pk=%s AND NOT (COALESCE(re.raw_payload,'{}'::jsonb)
                            ?| ARRAY['duplicate_merge','receipt_deleted'])
                        ORDER BY re.is_primary_source DESC,re.id LIMIT 1''',(r['expense_pk'],))
                    if not evidence:
                        raise ValueError('Expense is missing, deleted or a duplicate')
                    if r['receipt_source_sha256'] not in (None,evidence[0]['source_sha256']):
                        raise ValueError('Receipt identity changed; reload before saving')
                    r['receipt_source_sha256']=evidence[0]['source_sha256']
                elif r['receipt_source_sha256'] and r['receipt_source_sha256'] not in {b['receipt_source_sha256'] for b in before}:
                    raise ValueError('A new receipt link needs a current expense selection')
            result = {'transaction_id': transaction_id, 'version': version + int(apply),
                      'allocations': after, 'applied': apply}
            if apply:
                cur.execute('DELETE FROM accounting.transaction_allocations WHERE transaction_id=%s', (transaction_id,))
                for r in after:
                    cur.execute('''INSERT INTO accounting.transaction_allocations
                        (transaction_id,allocation_number,amount,category,person,expense_pk,note,receipt_source_sha256)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)''', (transaction_id, *r.values()))
                cur.execute('''INSERT INTO accounting.transaction_allocation_audit
                    (transaction_id,version,actor,reason,before_allocations,after_allocations)
                    VALUES (%s,%s,%s,%s,%s,%s)''',
                            (transaction_id, version+1, actor, reason,
                             Jsonb(json.loads(json.dumps(before, default=str))),
                             Jsonb(json.loads(json.dumps(after, default=str)))))
            return result


def receipt_candidates(service, transaction):
    """Exact amount and bounded date suggestions; never modify receipt fields."""
    from ..web.receipt_fields import RECEIPT_DATETIME_SQL
    if transaction['amount'] <= 0 or transaction.get('currency','CAD')!='CAD':
        return ()
    rows = service._fetch(f'''
        SELECT DISTINCT ON (e.id) e.id AS expense_pk, COALESCE(re.merchant,e.store_name) AS merchant,
               re.raw_payload->>'store' AS store, {RECEIPT_DATETIME_SQL} AS transaction_datetime,
               CASE WHEN re.transaction_datetime IS NOT NULL THEN 'receipt extraction'
                    ELSE 'existing expense date; verify source' END AS date_source,
               re.card_last4, re.total, re.extraction_status,
               abs({RECEIPT_DATETIME_SQL}::date - %s::date) AS date_distance_days
          FROM budget.expenses e JOIN budget.receipt_evidence re ON re.expense_pk=e.id
         WHERE re.total=%s AND {RECEIPT_DATETIME_SQL} IS NOT NULL
           AND abs({RECEIPT_DATETIME_SQL}::date - %s::date)<=7
           AND COALESCE(re.raw_payload->>'source_archived','false')<>'true'
           AND NOT (COALESCE(re.raw_payload,'{{}}'::jsonb) ?| ARRAY['duplicate_merge','receipt_deleted'])
         ORDER BY e.id, re.is_primary_source DESC, re.id
    ''', (transaction['transaction_date'], transaction['amount'], transaction['transaction_date']))
    # Card disagreement prevents a suggestion; unknown card remains visible.
    return tuple(sorted((r for r in rows if not (r['card_last4'] and transaction['card_last4']
                         and r['card_last4'] != transaction['card_last4'])),
                        key=lambda r: (r['date_distance_days'], r['expense_pk'])))
