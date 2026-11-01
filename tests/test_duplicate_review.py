from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from home_budget_pipeline.receipts import duplicate_review as review
from home_budget_pipeline.receipts.deduplication_store import DuplicateResolution


def evidence(i, expense=None):
    return {'id': i, 'expense_pk': expense, 'source_sha256': str(i)*64,
            'source_reference': f'{i}.pdf', 'evidence_type': 'electronic',
            'merchant': 'OCR merchant' if i == 49 else 'Costco',
            'transaction_datetime': datetime(2026, 1, 2, 12, 30),
            'total': Decimal('12.34'), 'receipt_id': None, 'card_last4': None}


@pytest.fixture
def state():
    return {'evidence': [evidence(45, 45), evidence(49)], 'decision': None}


@pytest.fixture
def db(monkeypatch, state):
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = {'id': 45}
    monkeypatch.setattr(review, 'read_state', lambda *a, **k: deepcopy(state))
    store = MagicMock()
    store.confirm_link.return_value = DuplicateResolution(45, 49, 45, 'confirmed')
    store.reject.return_value = DuplicateResolution(45, 49, None, 'rejected')
    monkeypatch.setattr(review, 'PostgresReceiptDuplicateStore', lambda *a, **k: store)
    return conn, cur, store


def call(db, **kwargs):
    return review.resolve(lambda: db[0], 45, 49, expense=45, reason='Paul verified both originals',
                          actor='Paul La Rocque', **kwargs)


def test_preview_is_read_only_and_never_calls_store(db, state):
    result = call(db)
    assert result['version'] == review.version(state)
    db[1].execute.assert_any_call('SET TRANSACTION READ ONLY')
    assert not db[2].mock_calls
    db[0].commit.assert_not_called()
    db[0].close.assert_called_once()


def test_stale_preview_rolls_back_without_linking(db):
    with pytest.raises(ValueError, match='changed'):
        call(db, apply=True, expected_version='a'*64)
    db[2].confirm_link.assert_not_called()
    db[0].rollback.assert_called_once()
    db[0].commit.assert_not_called()


@pytest.mark.parametrize('links', [(45, 99), (None, None), (99, None)])
def test_cannot_merge_or_reassign_existing_purchases(db, state, links):
    for row, link in zip(state['evidence'], links):
        row['expense_pk'] = link
    with pytest.raises(ValueError, match='merging or reassigning'):
        call(db, apply=True, expected_version=review.version(state))
    db[2].confirm_link.assert_not_called()
    db[0].commit.assert_not_called()


def test_human_confirmation_audits_low_score_without_inventing_score(db, state):
    result = call(db, apply=True, expected_version=review.version(state))
    score, expense = db[2].confirm_link.call_args.args
    assert score.score == 35
    assert 'human_review_of_low_score_pair' in score.reasons
    assert expense == 45 and result['resolution_status'] == 'confirmed'
    audit = [args for args, _ in db[1].execute.call_args_list if 'INSERT INTO lineage.receipt_events' in args[0]]
    assert len(audit) == 1
    payload = audit[0][1][-1].obj
    assert payload['actor_user'] == 'Paul La Rocque'
    assert payload['before']['evidence'] == payload['after']['evidence']
    db[0].commit.assert_called_once()


def test_audit_failure_rolls_back_link(db, state):
    def execute(sql, params=None):
        if 'INSERT INTO lineage.receipt_events' in sql:
            raise RuntimeError('audit unavailable')
    db[1].execute.side_effect = execute
    with pytest.raises(RuntimeError, match='audit unavailable'):
        call(db, apply=True, expected_version=review.version(state))
    db[0].rollback.assert_called_once()
    db[0].commit.assert_not_called()


def test_reject_records_decision_without_linking(db, state):
    result = call(db, reject=True, apply=True, expected_version=review.version(state))
    assert result['resolution_status'] == 'rejected'
    db[2].confirm_link.assert_not_called()
    db[2].reject.assert_called_once()


def test_repeating_same_confirmation_is_noop(db, state):
    state['decision'] = {'resolution_status': 'confirmed', 'canonical_expense_pk': 45}
    state['evidence'][1]['expense_pk'] = 45
    assert call(db, apply=True, expected_version='a'*64)['unchanged']
    db[2].confirm_link.assert_not_called()
    db[0].commit.assert_not_called()


def test_capture_keeps_low_score_metadata_match_pending_and_does_not_commit(monkeypatch):
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.side_effect = [evidence(49), None]
    cur.fetchall.return_value = [evidence(45, 45)]
    store = MagicMock()
    monkeypatch.setattr(review, 'PostgresReceiptDuplicateStore', lambda *a, **k: store)
    results = review.capture_candidates(conn, 49, allow_auto=True)
    store.auto_link_exact.assert_not_called()
    assert len(results) == 1 and results[0].score == 35
    assert results[0].disposition.value == 'review'
    store.save_score.assert_called_once()
    conn.commit.assert_not_called()


def test_capture_does_not_reopen_rejected_pair(monkeypatch):
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.side_effect = [evidence(49), {'resolution_status': 'rejected'}]
    cur.fetchall.return_value = [evidence(45, 45)]
    store = MagicMock()
    monkeypatch.setattr(review, 'PostgresReceiptDuplicateStore', lambda *a, **k: store)
    assert not review.capture_candidates(conn, 49)
    store.save_score.assert_not_called()


def test_duplicate_api_requires_identity_and_action_header(monkeypatch):
    from home_budget_pipeline.web.app import app, BASE_PATH
    monkeypatch.delenv('LEDGER_PROXY_SECRET', raising=False)
    with TestClient(app) as client:
        path = BASE_PATH + '/api/duplicates/resolve'
        data = {'left': 45, 'right': 49, 'expense': 45, 'reason': 'verified'}
        assert client.post(path, json=data).status_code == 401
        assert client.post(path, json=data, headers={'X-Forwarded-User': 'paul'}).status_code == 403


def test_confirmed_import_link_is_not_rematched_or_replaced(monkeypatch):
    from home_budget_pipeline.receipts import parallel_ingest as parallel
    from types import SimpleNamespace
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (45,)
    monkeypatch.setattr(parallel, 'resolve_merchant_alias', lambda *a: 'Costco')
    monkeypatch.setattr(parallel, 'upsert_evidence', lambda *a, **k: 49)
    matcher = MagicMock()
    canonical = MagicMock()
    monkeypatch.setattr(parallel, 'find_match', matcher)
    monkeypatch.setattr(parallel.scan, 'upsert_receipt', canonical)
    result = parallel.persist_evidence_first(conn, [SimpleNamespace(merchant='Costco')], 'budget')
    assert result['matched'] == 1
    matcher.assert_not_called()
    canonical.assert_not_called()


def test_import_auto_links_only_one_exact_canonical_target(monkeypatch):
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    source, other = evidence(49), evidence(45, 45)
    for row in (source, other):
        row.update(merchant='Costco', receipt_id='INV-123456', card_last4='1234')
    cur.fetchone.side_effect = [source, None]
    cur.fetchall.return_value = [other]
    store = MagicMock()
    monkeypatch.setattr(review, 'PostgresReceiptDuplicateStore', lambda *a, **k: store)
    monkeypatch.setattr(review, 'read_state', lambda *a, **k: {'evidence': [other, source], 'decision': None})
    results = review.capture_candidates(conn, 49, allow_auto=True)
    assert results[0].disposition.value == 'exact_duplicate'
    store.auto_link_exact.assert_called_once_with(results[0], 45)
    assert any('INSERT INTO lineage.receipt_events' in a[0] for a, _ in cur.execute.call_args_list)
    conn.commit.assert_not_called()


def test_ambiguous_exact_targets_never_auto_link(monkeypatch):
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    source, first, second = evidence(49), evidence(45, 45), evidence(46, 46)
    for row in (source, first, second):
        row.update(merchant='Costco', receipt_id='INV-123456', card_last4='1234')
    cur.fetchone.side_effect = [source, None, None]
    cur.fetchall.return_value = [first, second]
    store = MagicMock()
    monkeypatch.setattr(review, 'PostgresReceiptDuplicateStore', lambda *a, **k: store)
    assert len(review.capture_candidates(conn, 49, allow_auto=True)) == 2
    store.auto_link_exact.assert_not_called()


def test_import_does_not_create_purchase_for_unresolved_candidate(monkeypatch):
    from home_budget_pipeline.receipts import parallel_ingest as parallel
    from types import SimpleNamespace
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (None,)
    monkeypatch.setattr(parallel, 'resolve_merchant_alias', lambda *a: 'OCR merchant')
    monkeypatch.setattr(parallel, 'upsert_evidence', lambda *a, **k: 49)
    monkeypatch.setattr(parallel, 'capture_candidates', lambda *a, **k: ['review candidate'])
    monkeypatch.setattr(parallel, 'find_match', lambda *a: SimpleNamespace(disposition='new', expense_pk=None))
    canonical = MagicMock()
    monkeypatch.setattr(parallel.scan, 'upsert_receipt', canonical)
    result = parallel.persist_evidence_first(conn, [SimpleNamespace(merchant='OCR merchant')], 'budget')
    assert result['ambiguous'] == 1
    canonical.assert_not_called()


def test_refreshing_secondary_evidence_preserves_canonical_purchase(monkeypatch):
    from home_budget_pipeline.receipts import parallel_ingest as parallel
    from types import SimpleNamespace
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchone.side_effect = [(45,), ({}, 'canonical-sha')]
    monkeypatch.setattr(parallel, 'resolve_merchant_alias', lambda *a: 'Costco')
    monkeypatch.setattr(parallel, 'upsert_evidence', lambda *a, **k: 49)
    canonical = MagicMock()
    monkeypatch.setattr(parallel.scan, 'upsert_receipt', canonical)
    result = parallel.persist_evidence_first(conn,
        [SimpleNamespace(merchant='Costco', source_sha256='secondary-sha')], 'budget', replace_existing=True)
    assert result['matched'] == 1
    canonical.assert_not_called()


def test_reject_cannot_silently_split_already_linked_documents(db, state):
    state['evidence'][1]['expense_pk'] = 45
    with pytest.raises(ValueError, match='cannot split'):
        call(db, reject=True, apply=True, expected_version=review.version(state))
    db[2].reject.assert_not_called()
    db[0].commit.assert_not_called()
