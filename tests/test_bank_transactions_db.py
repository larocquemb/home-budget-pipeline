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


def test_tree_preview_apply_and_overlap_counts_agree_without_partial_import(bank_db,tmp_path):
    from home_budget_pipeline.transactions.tree_import import import_tree
    connect,_=bank_db
    root=tmp_path/'cibc'
    a=root/'smart/x1234/2026/one.csv';a.parent.mkdir(parents=True)
    a.write_text('2026-03-12,ATM,25.00,\n')
    b=a.with_name('overlap.csv');b.write_text(a.read_text()+'2026-03-13,DEPOSIT,,40.00\n')
    c=root/'eadvantage/x5678/two.csv';c.parent.mkdir(parents=True);c.write_text('2026-03-12,INTEREST,,0.05\n')
    with connect() as conn:
        preview=import_tree(conn,root,'Test')
        assert (preview['rows'],preview['new'],preview['already_imported'])==(4,3,1)
        assert conn.execute('SELECT count(*) AS n FROM accounting.accounts').fetchone()['n']==0
        applied=import_tree(conn,root,'Test',apply=True)
        assert applied['new']==preview['new']
        assert import_tree(conn,root,'Test',apply=True)['new']==0
        assert conn.execute('SELECT count(*) AS n FROM accounting.financial_transactions WHERE card_last4 IS NOT NULL').fetchone()['n']==0
        assert conn.execute('SELECT count(*) AS n FROM accounting.account_numbers').fetchone()['n']==2
        assert conn.execute('SELECT count(*) AS n FROM accounting.account_ownerships').fetchone()['n']==0
        c.write_text('bad file\n')
        with pytest.raises(ValueError):
            import_tree(conn,root,'Test',apply=True)
        assert conn.execute('SELECT count(*) AS n FROM accounting.financial_transactions').fetchone()['n']==3


def test_tree_reuses_existing_primary_card_account_reference(bank_db,tmp_path):
    from home_budget_pipeline.transactions.tree_import import import_tree
    from home_budget_pipeline.transactions.accounts import record_ownership
    connect,source=bank_db
    with connect() as conn:
        import_cibc(conn,source,'Existing stable label','Test',apply=True)
        record_ownership(conn,'Existing stable label','5678','Primary',{'5678':'Primary'},'Test')
        file=tmp_path/'cibc/mastercard/x5678/2026/card.csv';file.parent.mkdir(parents=True);file.write_bytes(source.read_bytes())
        report=import_tree(conn,tmp_path/'cibc','Test')
        assert report['new']==0
        assert report['files'][0]['account_reference']=='Existing stable label'


def test_usd_account_retains_currency_and_never_matches_cad_receipts(bank_db,tmp_path):
    from home_budget_pipeline.transactions.tree_import import import_tree
    connect,source=bank_db
    file=tmp_path/'cibc/visa/x5678/2026/card.csv';file.parent.mkdir(parents=True);file.write_bytes(source.read_bytes())
    with connect() as conn:
        report=import_tree(conn,tmp_path/'cibc','Test',apply=True,currencies={'visa/x5678':'USD'})
        assert report['files'][0]['currency']=='USD'
        assert import_tree(conn,tmp_path/'cibc','Test')['new']==0
        tx=conn.execute('SELECT * FROM accounting.financial_transactions WHERE amount>0 LIMIT 1').fetchone()
        assert tx['currency']=='USD'
        assert receipt_candidates(LedgerQueryService(connect),tx)==()
        with pytest.raises(ValueError,match='conflicts'):
            import_tree(conn,tmp_path/'cibc','Test',currencies={'visa/x5678':'CAD'})
        with pytest.raises(ValueError,match='currency'):
            save_allocations(conn,tx['id'],[{'amount':'12.34','category':'Food','expense_pk':42}],0,'Test','Checked',apply=True,receipt_service=LedgerQueryService(connect))
        save_allocations(conn,tx['id'],[{'amount':'12.34','category':'Food'}],0,'Test','USD allocation',apply=True)
        assert conn.execute('SELECT currency FROM accounting.accounts').fetchone()['currency']=='USD'


def test_combined_acu_cibc_archive_preserves_institution_privacy_and_reimport(bank_db,tmp_path):
    from home_budget_pipeline.transactions.tree_import import import_tree
    connect,source=bank_db
    root=tmp_path/'raw'
    acu=root/'acu/chequing/x1234/2026/file.csv';acu.parent.mkdir(parents=True)
    identifier='99999999999999999999'
    acu.write_text('Unique ID,Date,Description,Amount,Balance\n'+
                   f'{identifier},"Oct 31, 2026",INTEREST,$0.25,$25.25\n'+
                   f'{identifier},"Oct 30, 2026",WITHDRAWAL,-$5.00,$25.00\n')
    card=root/'cibc/visa/x5678/2026/card.csv';card.parent.mkdir(parents=True);card.write_bytes(source.read_bytes())
    with connect() as conn:
        preview=import_tree(conn,root,'Test',currencies={'cibc/visa/x5678':'USD'})
        assert (preview['rows'],preview['new'])==(5,5)
        import_tree(conn,root,'Test',apply=True,currencies={'cibc/visa/x5678':'USD'})
        assert import_tree(conn,root,'Test')['new']==0
        accounts=conn.execute('SELECT institution,currency FROM accounting.accounts ORDER BY institution').fetchall()
        assert accounts==[{'institution':'ACU','currency':'CAD'},{'institution':'CIBC','currency':'USD'}]
        retained=conn.execute("SELECT raw_source FROM accounting.financial_transactions WHERE institution='ACU'").fetchall()
        assert all(identifier not in str(r) and r['raw_source']['redacted_columns']==['Unique ID'] for r in retained)
        snapshots=conn.execute('SELECT raw_row FROM accounting.transaction_import_rows').fetchall()
        assert all(identifier not in str(r) for r in snapshots)


def test_account_nickname_changes_preserve_identity_currency_ownership_and_fingerprints(bank_db):
    from home_budget_pipeline.transactions.accounts import register_account,record_ownership
    connect,source=bank_db
    with connect() as conn:
        import_cibc(conn,source,'Stable account reference','Test',apply=True)
        record_ownership(conn,'Stable account reference','5678','Primary',{'5678':'Primary'},'Test')
        original=conn.execute('SELECT id,import_fingerprint FROM accounting.financial_transactions ORDER BY id').fetchall()
        first=register_account(conn,'Stable account reference','CIBC','credit_card','5678','Friendly label','Test')
        second=register_account(conn,'Stable account reference','CIBC','credit_card','5678','Renamed label','Test')
        assert first['account_id']==second['account_id']
        assert conn.execute('SELECT id,import_fingerprint FROM accounting.financial_transactions ORDER BY id').fetchall()==original
        assert import_cibc(conn,source,'Stable account reference','Test')['new']==0
        assert conn.execute('SELECT count(*) AS n FROM accounting.account_ownerships').fetchone()['n']==1
        assert conn.execute('SELECT count(*) AS n FROM accounting.account_profile_audit').fetchone()['n']==2
        with pytest.raises(ValueError,match='currency'):
            register_account(conn,'Stable account reference','CIBC','credit_card','5678','Wrong currency','Test',currency='USD')
        assert conn.execute('SELECT nickname FROM accounting.accounts').fetchone()['nickname']=='Renamed label'


def test_us_institution_and_canadian_us_account_folder_import_as_usd(bank_db,tmp_path):
    from home_budget_pipeline.transactions.tree_import import import_tree
    connect,_=bank_db
    root=tmp_path/'raw'
    ca=root/'cibc/us/x1234/2026/ca.csv';ca.parent.mkdir(parents=True);ca.write_text('2026-05-11,TRANSFER,25.00,\n')
    us=root/'cibcus/smart/x5678/2026/us.csv';us.parent.mkdir(parents=True)
    us.write_text('Transaction Date,Short Description,Long Description,Amount\n2026-05-11,TRANSFER,TO 123456789012,-25.00\n')
    with connect() as conn:
        report=import_tree(conn,root,'Test',apply=True)
        assert report['new']==2
        assert all(r['currency']=='USD' for r in report['files'])
        assert import_tree(conn,root,'Test')['new']==0
        rows=conn.execute('SELECT institution,currency,raw_source FROM accounting.financial_transactions ORDER BY id').fetchall()
        assert {r['institution'] for r in rows}=={'CIBC','CIBC US'}
        assert all(r['currency']=='USD' for r in rows)
        assert '123456789012' not in str(rows)
