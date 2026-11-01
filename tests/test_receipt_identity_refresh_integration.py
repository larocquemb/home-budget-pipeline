"""Metadata refresh is audited/idempotent and cannot rewrite financial history."""
import json
import os
from types import SimpleNamespace
import uuid

import psycopg
from psycopg.rows import dict_row
import pytest

from home_budget_pipeline.receipts import identities
from home_budget_pipeline.web.queries import LedgerQueryService

pytestmark=pytest.mark.integration


def test_identity_refresh_preserves_canonical_and_items_and_exposes_store(capsys):
    dsn=os.environ['TEST_DATABASE_URL'];sha=uuid.uuid4().hex+uuid.uuid4().hex
    with psycopg.connect(dsn,row_factory=dict_row) as conn:
        pk=conn.execute('''INSERT INTO budget.expenses(source,order_id,store_name,expense_total,raw_payload)
            VALUES ('scanned',%s,'ExampleMart',12.34,'{"item_allocation_correction":{"verified":true}}') RETURNING id''',(sha,)).fetchone()['id']
        conn.execute("INSERT INTO budget.expense_items(expense_pk,item_name,line_total) VALUES (%s,'Verified product',12.34)",(pk,))
        conn.execute('''INSERT INTO budget.receipt_evidence(expense_pk,evidence_type,source_reference,source_sha256,merchant,raw_text,raw_payload)
            VALUES (%s,'electronic',%s,%s,'NORTHVILLE #987',%s,'{"retained":true}')''',
            (pk,sha+'.pdf',sha,'NORTHVILLE #987\nCHIP\nXXXXXXXX4321\nMastercard 12.34'))
        before=conn.execute('SELECT * FROM budget.expenses WHERE id=%s',(pk,)).fetchone()
        items=conn.execute('SELECT * FROM budget.expense_items WHERE expense_pk=%s',(pk,)).fetchall()
    args=SimpleNamespace(db_dsn=dsn,source_reference=sha+'.pdf',apply=False,expected_version=None)
    try:
        identities.run(args);plan=json.loads(capsys.readouterr().out)
        assert len(plan['changes'])==1
        args.apply=True;args.expected_version='stale'
        with pytest.raises(ValueError):identities.run(args)
        args.expected_version=plan['version'];identities.run(args);capsys.readouterr()
        args.apply=False;identities.run(args);assert not json.loads(capsys.readouterr().out)['changes']
        with psycopg.connect(dsn,row_factory=dict_row) as conn:
            assert conn.execute('SELECT * FROM budget.expenses WHERE id=%s',(pk,)).fetchone()==before
            assert conn.execute('SELECT * FROM budget.expense_items WHERE expense_pk=%s',(pk,)).fetchall()==items
            ev=conn.execute('SELECT * FROM budget.receipt_evidence WHERE source_sha256=%s',(sha,)).fetchone()
            assert ev['merchant']=='ExampleMart' and ev['raw_payload']['store']=='NORTHVILLE #987'
            assert ev['raw_payload']['retained'] and ev['card_last4']=='4321'
            assert conn.execute('SELECT count(*) FROM lineage.receipt_events WHERE source_sha256=%s',(sha,)).fetchone()['count']==1
        service=LedgerQueryService(lambda:psycopg.connect(dsn,row_factory=dict_row))
        detail=service.expense_detail(pk)
        assert detail['merchant']=='ExampleMart' and detail['store']=='NORTHVILLE #987'
        assert service.receipt_evidence(ev['id'])['store']=='NORTHVILLE #987'
    finally:
        with psycopg.connect(dsn) as conn:
            conn.execute('DELETE FROM lineage.receipt_events WHERE source_sha256=%s',(sha,))
            conn.execute('DELETE FROM budget.receipt_evidence WHERE source_sha256=%s',(sha,))
            conn.execute('DELETE FROM budget.expenses WHERE id=%s',(pk,))
