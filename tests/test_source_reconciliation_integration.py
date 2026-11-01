"""Metadata-only source reconciliation against the explicit disposable CI DB."""
import os
import uuid
from hashlib import sha256

import psycopg
from psycopg.rows import dict_row
import pytest
from home_budget_pipeline.receipts import source_reconciliation as sources

pytestmark = pytest.mark.integration


def test_real_source_moves_archives_and_restore_preserve_purchase(tmp_path, monkeypatch):
    # The public command's dev-identity refusal is unit-tested. Exercise its SQL
    # on CI's disposable TEST_DATABASE_URL, never on an application database.
    monkeypatch.setattr(sources, 'verify_development', lambda conn: None)
    connect=lambda:psycopg.connect(os.environ['TEST_DATABASE_URL'],row_factory=dict_row)
    folder=tmp_path/'batch';folder.mkdir()
    data=[uuid.uuid4().bytes for _ in range(3)];hashes=[sha256(b).hexdigest() for b in data]
    (folder/'new.pdf').write_bytes(data[0]);(folder/'replaced.pdf').write_bytes(b'replacement')
    ids=[];expense=None
    try:
        with connect() as conn:
            expense=conn.execute("INSERT INTO budget.expenses(source,order_id,expense_total) VALUES ('scanned',%s,12.34) RETURNING id",(str(uuid.uuid4()),)).fetchone()['id']
            for name,sha in zip(('old.pdf','missing.pdf','replaced.pdf'),hashes):
                ids.append(conn.execute("INSERT INTO budget.receipt_evidence(expense_pk,evidence_type,source_reference,source_sha256,raw_text,raw_payload) VALUES (%s,'electronic',%s,%s,'original text','{\"retained\":true}') RETURNING id",(expense,'electronic/batch/'+name,sha)).fetchone()['id'])
            before=conn.execute('SELECT * FROM budget.expenses WHERE id=%s',(expense,)).fetchone()
        args=dict(scanned_root='',electronic_root=str(tmp_path),actor='Test Reviewer')
        preview=sources.reconcile(connect,'electronic/batch/',**args)
        assert [c['action'] for c in preview['changes']]==['relocate','archive','archive']
        with pytest.raises(ValueError,match='current preview'):
            sources.reconcile(connect,'electronic/batch/',**args,apply=True,expected_version='stale')
        result=sources.reconcile(connect,'electronic/batch/',**args,apply=True,expected_version=preview['version'])
        assert len(result['audit_ids'])==3
        assert not sources.reconcile(connect,'electronic/batch/',**args)['changes']
        with connect() as conn:
            rows=conn.execute('SELECT * FROM budget.receipt_evidence WHERE id=ANY(%s) ORDER BY id',(ids,)).fetchall()
            assert rows[0]['source_reference']=='electronic/batch/new.pdf'
            assert [r['raw_payload']['source_archived'] for r in rows]==[False,True,True]
            assert all(r['raw_text']=='original text' and r['raw_payload']['retained'] for r in rows)
            assert conn.execute('SELECT * FROM budget.expenses WHERE id=%s',(expense,)).fetchone()==before
        (folder/'missing.pdf').write_bytes(data[1])
        preview=sources.reconcile(connect,'electronic/batch/',**args)
        assert preview['changes'][0]['action']=='restore'
        sources.reconcile(connect,'electronic/batch/',**args,apply=True,expected_version=preview['version'])
    finally:
        with connect() as conn:
            conn.execute('DELETE FROM lineage.receipt_events WHERE source_sha256=ANY(%s)',(hashes,))
            conn.execute('DELETE FROM budget.receipt_evidence WHERE source_sha256=ANY(%s)',(hashes,))
            if expense is not None:conn.execute('DELETE FROM budget.expenses WHERE id=%s',(expense,))
