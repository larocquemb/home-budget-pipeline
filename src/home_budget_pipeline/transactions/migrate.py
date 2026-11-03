"""Copy legacy bank records without deleting originals or rebuilding receipts."""
from psycopg.types.json import Jsonb
from .workflows import ensure_schema

TABLES=('financial_transactions','transaction_imports','transaction_import_rows',
        'transaction_allocations','transaction_allocation_audit')


def migrate(receipts, accounting, *, apply=False):
    snapshot={}
    with receipts.transaction():
        receipts.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
        for table in TABLES:
            snapshot[table]=receipts.execute('SELECT * FROM budget.'+table).fetchall()
        # Resolve old expense numbers to immutable source identities before copying.
        for row in snapshot['transaction_allocations']:
            if row['expense_pk'] is not None:
                evidence=receipts.execute('''SELECT source_sha256 FROM budget.receipt_evidence
                    WHERE expense_pk=%s ORDER BY is_primary_source DESC,id LIMIT 1''',(row['expense_pk'],)).fetchone()
                if not evidence:
                    raise ValueError('Legacy allocation has no retained source; migration requires review')
                row['receipt_source_sha256']=evidence['source_sha256']
            else:
                row['receipt_source_sha256']=None
    report={'source_counts':{k:len(v) for k,v in snapshot.items()},'applied':apply}
    if not apply:
        return report
    with accounting.transaction():
        ensure_schema(accounting)
        for row in snapshot['financial_transactions']:
            accounting.execute('''INSERT INTO accounting.accounts(account_reference,institution)
                VALUES(%s,%s) ON CONFLICT(account_reference) DO NOTHING''',(row['source_account_reference'],row['institution']))
            account=accounting.execute('SELECT id FROM accounting.accounts WHERE account_reference=%s',(row['source_account_reference'],)).fetchone()['id']
            row['account_id']=account
            if row['card_last4']:
                accounting.execute('''INSERT INTO accounting.account_numbers(account_id,number_kind,last4)
                    VALUES(%s,'card',%s) ON CONFLICT DO NOTHING''',(account,row['card_last4']))
        for table,rows in snapshot.items():
            for row in rows:
                from psycopg import sql
                columns=list(row)
                statement=sql.SQL('INSERT INTO accounting.{} ({}) VALUES ({}) ON CONFLICT DO NOTHING').format(
                    sql.Identifier(table),sql.SQL(',').join(map(sql.Identifier,columns)),
                    sql.SQL(',').join(sql.Placeholder() for _ in columns))
                accounting.execute(statement,tuple(Jsonb(v) if isinstance(v,(dict,list)) else v for v in row.values()))
            actual=accounting.execute('SELECT * FROM accounting.'+table).fetchall()
            # Never declare a conflicting destination a successful migration.
            def keyed(values):
                import json
                return sorted(json.dumps(v,sort_keys=True,default=str) for v in values)
            if keyed(actual)!=keyed(rows):
                raise ValueError('Destination differs from source; migration rolled back: '+table)
            if rows and 'id' in rows[0]:
                accounting.execute('SELECT setval(pg_get_serial_sequence(%s,\'id\'),(SELECT max(id) FROM accounting.'+table+'),true)',('accounting.'+table,))
    return {**report,'all_rows_verified':True,'legacy_source':'preserved; no longer active after accounting connection configured'}
