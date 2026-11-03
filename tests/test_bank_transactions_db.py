"""Real transactional checks; run only in a dedicated disposable database."""
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import pytest

from home_budget_pipeline.transactions.workflows import import_cibc, ensure_schema, save_allocations, receipt_candidates
from home_budget_pipeline.web.queries import LedgerQueryService

pytestmark = pytest.mark.integration


@pytest.fixture
def bank_db(tmp_path):
    import psycopg
    from psycopg.rows import dict_row
    dsn = os.environ['TEST_DATABASE_URL']
    def connect():
        return psycopg.connect(dsn, row_factory=dict_row)
    with connect() as conn:
        if conn.execute('SELECT current_database() AS db').fetchone()['db'] != 'home_budget_transactions_test':
            pytest.skip('Bank workflow checks require a dedicated disposable home_budget_transactions_test database')
        conn.execute('DROP SCHEMA IF EXISTS accounting CASCADE')
        conn.execute('DROP SCHEMA IF EXISTS budget CASCADE')
        conn.execute(Path('sql/schema_phase1.sql').read_text())
        conn.execute(Path('sql/financial_transactions.sql').read_text())
        ensure_schema(conn)
        ensure_schema(conn)
    source = tmp_path/'bank.csv'
    source.write_text('2026-03-12,MARKET MAIN,12.34,,1234********5678\n'*2 +
                      '2026-03-13,PAYMENT THANK YOU,,20.00,1234********5678\n')
    yield connect, source
    with connect() as conn:
        conn.execute('DROP SCHEMA budget CASCADE')
        conn.execute('DROP SCHEMA accounting CASCADE')


def test_atomic_import_repeat_overlap_provenance_and_multiplicity(bank_db, tmp_path):
    connect, source = bank_db
    with connect() as conn:
        assert import_cibc(conn, source, 'Test card', 'Test', apply=False)['new'] == 3
        assert conn.execute('SELECT count(*) AS n FROM accounting.transaction_imports').fetchone()['n'] == 0
        assert import_cibc(conn, source, 'Test card', 'Test', apply=True)['new'] == 3
        assert import_cibc(conn, source, 'Test card', 'Test', apply=True)['new'] == 0
        overlap=tmp_path/'overlap.csv'
        overlap.write_text(source.read_text()+'2026-03-14,OTHER,7.50,,1234********5678\n')
        assert import_cibc(conn, overlap, 'Test card', 'Test', apply=True)['new'] == 1
        assert conn.execute('SELECT count(*) AS n FROM accounting.financial_transactions').fetchone()['n'] == 4
        assert conn.execute('SELECT count(*) AS n FROM accounting.transaction_import_rows').fetchone()['n'] == 7
        source.write_text(source.read_text()+'bad row\n')
        with pytest.raises(ValueError):
            import_cibc(conn,source,'Test card','Test',apply=True)
        assert conn.execute('SELECT count(*) AS n FROM accounting.financial_transactions').fetchone()['n'] == 4


def test_split_audit_rollback_stale_version_and_payment_exclusion(bank_db):
    connect, source = bank_db
    with connect() as conn:
        import_cibc(conn,source,'Test card','Test',apply=True)
        ids = conn.execute('SELECT id,amount FROM accounting.financial_transactions ORDER BY id').fetchall()
    txid = ids[0]['id']
    rows=[{'amount':'2.34','category':'Home'}, {'amount':'10.00','category':'Food'}]
    with connect() as conn:
        assert not save_allocations(conn,txid,rows,0,'Test','Preview')['applied']
        assert conn.execute('SELECT count(*) AS n FROM accounting.transaction_allocations').fetchone()['n'] == 0
        assert save_allocations(conn,txid,rows,0,'Test','Confirmed',apply=True)['version'] == 1
        with pytest.raises(ValueError, match='changed'):
            save_allocations(conn,txid,rows,0,'Test','Stale',apply=True)
        with pytest.raises(ValueError, match='sum exactly'):
            save_allocations(conn,txid,[rows[0]],1,'Test','Unbalanced',apply=True)
        with pytest.raises(ValueError, match='transfers'):
            save_allocations(conn,ids[-1]['id'],[{'amount':'-20.00','category':'Food'}],0,'Test','Wrong',apply=True)
        assert conn.execute('SELECT count(*) AS n FROM accounting.transaction_allocation_audit').fetchone()['n'] == 1
    def write():
        try:
            with connect() as conn:
                save_allocations(conn,txid,rows,1,'Test','Concurrent',apply=True)
            return 'saved'
        except ValueError:
            return 'stale'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: write(), range(2))) == ['saved','stale']


def test_database_enforces_balance_even_for_direct_sql(bank_db):
    import psycopg
    connect, source = bank_db
    with connect() as conn:
        import_cibc(conn,source,'Test card','Test',apply=True)
        txid=conn.execute('SELECT min(id) AS id FROM accounting.financial_transactions').fetchone()['id']
    with pytest.raises(psycopg.errors.RaiseException, match='must balance'):
        with connect() as conn:
            conn.execute("INSERT INTO accounting.transaction_allocations(transaction_id,allocation_number,amount,category) VALUES (%s,1,1.00,'Food')",(txid,))


def test_receipt_suggestions_are_read_only_and_respect_card_and_disposition(bank_db):
    connect, source = bank_db
    with connect() as conn:
        import_cibc(conn,source,'Test card','Test',apply=True)
        conn.execute("INSERT INTO budget.expenses (source,order_id) VALUES ('scanned','synthetic')")
        conn.execute('''INSERT INTO budget.receipt_evidence
            (expense_pk,evidence_type,source_reference,source_sha256,merchant,total,transaction_datetime,card_last4)
            SELECT id,'scanned','synthetic.pdf',repeat('a',64),'Market',12.34,'2026-03-10', '5678'
            FROM budget.expenses''')
        tx=conn.execute('SELECT * FROM accounting.financial_transactions WHERE amount>0 LIMIT 1').fetchone()
    service=LedgerQueryService(connect)
    assert len(receipt_candidates(service,tx)) == 1
    assert not receipt_candidates(service,{**tx,'card_last4':'9012'})
    with connect() as conn:
        conn.execute("UPDATE budget.expenses SET order_date='2026-03-10'")
        conn.execute('UPDATE budget.receipt_evidence SET transaction_datetime=NULL')
    assert receipt_candidates(service,tx)[0]['date_source'] == 'existing expense date; verify source'
    with connect() as conn:
        conn.execute('''UPDATE budget.receipt_evidence
            SET raw_payload='{"field_decisions":{"transaction_datetime":{"status":"conflict"}}}' ''')
    assert not receipt_candidates(service,tx)

    with connect() as conn:
        conn.execute('''UPDATE budget.receipt_evidence SET raw_payload='{"receipt_deleted":{}}' ''')
    assert not receipt_candidates(service,tx)


def test_last_four_ownership_is_idempotent_and_has_no_invented_effective_dates(bank_db):
    from home_budget_pipeline.transactions.accounts import record_ownership
    connect,source=bank_db
    with connect() as conn:
        import_cibc(conn,source,'Test card','Test',apply=True)
        for _ in range(2):
            record_ownership(conn,'Test card','5678','Primary',{'5678':'Primary','9012':'Additional'},'Reviewer')
        rows=conn.execute('SELECT owner_name,ownership_role,effective_from FROM accounting.account_ownerships').fetchall()
        assert len(rows)==2
        assert all(r['effective_from'] is None for r in rows)
        assert conn.execute('SELECT count(*) AS n FROM accounting.account_numbers').fetchone()['n']==2
        assert conn.execute('SELECT count(*) AS n FROM accounting.financial_transactions WHERE payer IS NOT NULL').fetchone()['n']==0
        with pytest.raises(ValueError,match='four'):
            record_ownership(conn,'Test card','1234567890125678','Primary',{'5678':'Primary'},'Reviewer')
        record_ownership(conn,'Test card','5678','Other',{'5678':'Other','9012':'Additional'},'Reviewer',reason='Confirmed correction')
        assert conn.execute('SELECT count(*) AS n FROM accounting.account_ownerships').fetchone()['n']==2
        audits=conn.execute('SELECT * FROM accounting.account_ownership_audit ORDER BY id').fetchall()
        assert len(audits)==2
        assert audits[-1]['before_ownerships'][0]['owner_name']=='Primary'
        assert audits[-1]['after_ownerships'][0]['owner_name']=='Other'


def test_bank_allocations_survive_receipt_schema_drop_and_recycled_expense_id(bank_db):
    connect,source=bank_db
    with connect() as conn:
        import_cibc(conn,source,'Test card','Test',apply=True)
        txid=conn.execute('SELECT min(id) AS id FROM accounting.financial_transactions').fetchone()['id']
        conn.execute("INSERT INTO budget.expenses(id,source,order_id) VALUES(42,'scanned','synthetic')")
        conn.execute("""INSERT INTO budget.receipt_evidence(expense_pk,evidence_type,source_reference,source_sha256)
            VALUES(42,'scanned','synthetic.pdf',repeat('a',64))""")
    plan=[{'amount':'12.34','category':'Food','expense_pk':42}]
    with connect() as conn:
        save_allocations(conn,txid,plan,0,'Reviewer','Checked source',apply=True,receipt_service=LedgerQueryService(connect))
    with connect() as conn:
        conn.execute('DROP SCHEMA budget CASCADE')
        conn.execute(Path('sql/schema_phase1.sql').read_text())
        conn.execute("INSERT INTO budget.expenses(id,source,order_id) VALUES(42,'scanned','different')")
        conn.execute("""INSERT INTO budget.receipt_evidence(expense_pk,evidence_type,source_reference,source_sha256)
            VALUES(42,'scanned','different.pdf',repeat('b',64))""")
        assert conn.execute('SELECT count(*) AS n FROM accounting.financial_transactions').fetchone()['n']==3
        saved=conn.execute('SELECT * FROM accounting.transaction_allocations').fetchone()
        assert saved['receipt_source_sha256']=='a'*64
    stale=[{'amount':'12.34','category':'Food','expense_pk':42,'receipt_source_sha256':'a'*64}]
    with connect() as conn:
        with pytest.raises(ValueError,match='identity changed'):
            save_allocations(conn,txid,stale,1,'Reviewer','Stale receipt id',apply=True,receipt_service=LedgerQueryService(connect))


def test_legacy_migration_copies_and_verifies_without_deleting_source(bank_db):
    from home_budget_pipeline.transactions.migrate import migrate
    connect,source=bank_db
    with connect() as conn:
        conn.execute(Path('sql/transaction_workflows.sql').read_text())
        conn.execute("""INSERT INTO budget.financial_transactions
          (institution,source_account_reference,transaction_date,amount,description,card_last4,import_fingerprint,raw_source)
          VALUES('CIBC','Legacy card','2026-03-12',12.34,'Synthetic','5678','synthetic-fingerprint','{}')""")
    with connect() as source_conn,connect() as destination:
        result=migrate(source_conn,destination,apply=True)
        assert result['all_rows_verified'] is True
    with connect() as conn:
        assert conn.execute('SELECT count(*) AS n FROM budget.financial_transactions').fetchone()['n']==1
        assert conn.execute('SELECT count(*) AS n FROM accounting.financial_transactions').fetchone()['n']==1
