"""Reconcile source locations and reversible archive flags; never delete history."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import uuid


def file_hash(path):
    before = path.stat()
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError('Source file changed during inspection; retry reconciliation')
    return digest.hexdigest()


def inventory(prefix, scanned_root, electronic_root):
    parts = PurePosixPath(prefix)
    if (not prefix.endswith('/') or parts.is_absolute() or '..' in parts.parts
            or len(parts.parts) < (2 if prefix.startswith('electronic/') else 1)):
        raise ValueError('Specify a relative source folder ending in /')
    electronic = prefix.startswith('electronic/')
    root_value = electronic_root if electronic else scanned_root
    if not root_value:
        raise ValueError('Configure the selected receipt source root')
    root = Path(root_value).resolve()
    local_prefix = prefix.removeprefix('electronic/') if electronic else prefix
    folder = (root / local_prefix).resolve()
    if root not in folder.parents or not folder.is_dir():
        raise ValueError('Source folder unavailable; refusing to archive records')
    result = {}
    for path in sorted(folder.rglob('*')):
        if not path.is_file() or path.name.startswith('.'):
            continue
        if folder not in path.resolve().parents:
            raise ValueError('Source file escapes the selected folder')
        reference = ('electronic/' if electronic else '') + path.relative_to(root).as_posix()
        result[reference] = file_hash(path)
    return result


def plan(rows, files):
    changes = []
    for row in rows:
        payload = row.get('raw_payload') or {}
        archived = payload.get('source_archived') is True
        reference, sha = row['source_reference'], row['source_sha256']
        matches = sorted(ref for ref, current_sha in files.items() if current_sha == sha)
        desired = reference if files.get(reference) == sha else next(iter(matches), None)
        if desired is None:
            if not archived:
                changes.append({'evidence_id': row['id'], 'action': 'archive',
                    'reason': 'source_replaced' if reference in files else 'source_missing',
                    'old_reference': reference, 'new_reference': reference})
        elif desired != reference or archived:
            changes.append({'evidence_id': row['id'], 'action': 'restore' if archived else 'relocate',
                'reason': 'same_source_hash_found', 'old_reference': reference, 'new_reference': desired})
    return changes


def verify_development(conn):
    from psycopg.rows import tuple_row
    with conn.cursor(row_factory=tuple_row) as cur:
        identity = cur.execute("SELECT current_database(),current_user,current_setting('home_budget.environment',true)").fetchone()
        if identity != ('home_budget_dev', 'home_budget_dev', 'development'):
            raise ValueError('Source reconciliation requires the isolated development database')


def reconcile(connect, prefix, *, scanned_root, electronic_root, actor, apply=False, expected_version=None):
    from psycopg.rows import dict_row, tuple_row
    from psycopg.types.json import Jsonb
    files = inventory(prefix, scanned_root, electronic_root)
    conn = connect()
    try:
        # This command currently supports the positively identified development
        # database only; unavailable mounts must not retire production sources.
        with conn.cursor(row_factory=tuple_row) as cur:
            if not apply:
                cur.execute('SET TRANSACTION READ ONLY')
        verify_development(conn)
        with conn.cursor(row_factory=dict_row) as cur:
            if apply:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext('budget.receipt-duplicates'))")
            pattern = prefix.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
            cur.execute('SELECT * FROM budget.receipt_evidence WHERE source_reference LIKE %s ORDER BY id'
                        + (' FOR UPDATE' if apply else ''), (pattern,))
            rows = cur.fetchall()
            changes = plan(rows, files)
            token = hashlib.sha256(json.dumps({'rows': rows, 'files': files}, default=str, sort_keys=True).encode()).hexdigest()
            if not apply:
                return {'apply': False, 'version': token, 'source_files': len(files), 'changes': changes}
            if not actor.strip() or expected_version != token:
                raise ValueError('Apply requires a reviewer and the current preview version')
            # Check the directory snapshot again before making any metadata writes.
            if inventory(prefix, scanned_root, electronic_root) != files:
                raise ValueError('Source folder changed; preview again')
            by_id = {r['id']: r for r in rows}
            audits = []
            for change in changes:
                before = by_id[change['evidence_id']]
                payload = dict(before.get('raw_payload') or {})
                payload['source_archived'] = change['action'] == 'archive'
                cur.execute('UPDATE budget.receipt_evidence SET source_reference=%s,raw_payload=%s,updated_at=NOW() WHERE id=%s',
                    (change['new_reference'], Jsonb(payload), before['id']))
                if change['action'] != 'archive':
                    cur.execute('UPDATE ingest.receipts SET source_reference=%s,updated_at=NOW() WHERE source_sha256=%s',
                        (change['new_reference'], before['source_sha256']))
                audit = str(uuid.uuid4())
                event = {'event': 'receipt_source_reconciled', 'actor_user': actor, 'change': change,
                         'before': before, 'after_reference': change['new_reference'],
                         'after_source_archived': payload['source_archived']}
                cur.execute('INSERT INTO lineage.receipt_events(id,source_sha256,source_reference,payload) VALUES (%s,%s,%s,%s)',
                    (audit, before['source_sha256'], before['source_reference'],
                     Jsonb(json.loads(json.dumps(event, default=str)))))
                audits.append(audit)
        conn.commit()
        return {'apply': True, 'changes': changes, 'audit_ids': audits}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
