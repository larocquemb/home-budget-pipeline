from copy import deepcopy
from hashlib import sha256
from unittest.mock import MagicMock

from fastapi import HTTPException
import pytest

from home_budget_pipeline.receipts import source_reconciliation as sources


def row(i, ref='electronic/batch/old.pdf', sha='a'*64, archived=False):
    return {'id': i, 'source_reference': ref, 'source_sha256': sha,
            'raw_payload': {'source_archived': archived, 'original': 'retained'}, 'expense_pk': 42}


@pytest.mark.parametrize('record,files,action', [
    (row(1), {}, 'archive'),
    (row(1), {'electronic/batch/old.pdf': 'b'*64}, 'archive'),
    (row(1), {'electronic/batch/new.pdf': 'a'*64}, 'relocate'),
    (row(1, archived=True), {'electronic/batch/old.pdf': 'a'*64}, 'restore'),
    (row(1), {'electronic/batch/old.pdf': 'a'*64}, None),
    (row(1, archived=True), {}, None),
])
def test_hash_identity_drives_reversible_source_plan(record, files, action):
    before = deepcopy(record)
    result = sources.plan([record], files)
    assert (result[0]['action'] if result else None) == action
    assert record == before


def test_missing_folder_and_path_escape_are_refused(tmp_path):
    for prefix in ('electronic/missing/', 'electronic/../', '/absolute/', 'electronic/'):
        with pytest.raises(ValueError):
            sources.inventory(prefix, str(tmp_path), str(tmp_path))


def test_inventory_hashes_bytes_and_refuses_external_symlinks(tmp_path):
    folder = tmp_path/'batch';folder.mkdir()
    (folder/'one.pdf').write_bytes(b'original')
    assert sources.inventory('electronic/batch/', '', str(tmp_path)) == {
        'electronic/batch/one.pdf': sha256(b'original').hexdigest()}
    outside = tmp_path/'outside.pdf';outside.write_bytes(b'outside')
    (folder/'escape.pdf').symlink_to(outside)
    with pytest.raises(ValueError, match='escapes'):
        sources.inventory('electronic/batch/', '', str(tmp_path))


def test_other_database_is_refused():
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = ('home_budget', 'admin', None)
    with pytest.raises(ValueError, match='isolated development'):
        sources.verify_development(conn)
    conn.commit.assert_not_called()


def test_stale_preview_rolls_back_without_metadata_writes(tmp_path):
    (tmp_path/'batch').mkdir()
    conn = MagicMock();cur = conn.cursor.return_value.__enter__.return_value
    cur.execute.return_value.fetchone.return_value = ('home_budget_dev', 'home_budget_dev', 'development')
    cur.fetchall.return_value = [row(1)]
    with pytest.raises(ValueError, match='current preview'):
        sources.reconcile(lambda:conn, 'electronic/batch/', scanned_root='', electronic_root=str(tmp_path),
                          actor='Test Reviewer', apply=True, expected_version='stale')
    assert not any(a[0].startswith('UPDATE ') for a, _ in cur.execute.call_args_list)
    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()


def test_document_preview_refuses_reused_filename_and_archived_copy(tmp_path, monkeypatch):
    from home_budget_pipeline.web import app as web
    path = tmp_path/'receipt.pdf';path.write_bytes(b'new document')
    monkeypatch.setattr(web, 'RECEIPT_SOURCE_ROOT', tmp_path)
    evidence = {'source_reference':'receipt.pdf','source_sha256':sha256(b'old document').hexdigest()}
    with pytest.raises(HTTPException) as exc:
        web._verified_receipt_document(evidence)
    assert exc.value.status_code == 409
    evidence['raw_payload'] = {'source_archived':True}
    with pytest.raises(HTTPException) as exc:
        web._verified_receipt_document(evidence)
    assert exc.value.status_code == 410


def test_default_list_filters_archive_and_history_pagination_retains_choice():
    from home_budget_pipeline.web import receipt_app as web
    from home_budget_pipeline.web.queries import Page
    queries=[]
    class Service:
        def _fetch(self, sql, params):
            queries.append(sql)
            return ({'total_count':0},) if 'COUNT(*)' in sql else ()
    web._receipt_list(Service(),limit=10,offset=0)
    assert "COALESCE(re.raw_payload->>'source_archived','false') <> 'true'" in queries[0]
    queries.clear()
    web._receipt_list(Service(),limit=10,offset=0,include_archived=True)
    assert "<> 'true'" not in queries[0]
    class PageService:
        def _fetch(self,sql,params):
            return ({'total_count':30},) if 'COUNT(*)' in sql else ()
    html=web.receipts_page(limit=10,offset=0,include_archived=True,service=PageService(),identity={'user':'test'})
    assert 'Show archived or deleted receipts' in html and 'include_archived=true' in html
