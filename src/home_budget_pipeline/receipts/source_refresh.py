"""Reconcile active DB sources and retire stale caches with retained evidence."""
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import uuid

from . import source_reconciliation as sources
from .message import ReceiptMessage
from .queue_ingest import cache_rebuild_lock


def stale_caches(cache_root, prefix, files, source_references=None):
    root = Path(cache_root).expanduser().resolve()
    changes = []
    if not root.is_dir():
        return changes
    for path in sorted(root.rglob('*.json')):
        relative = path.relative_to(root)
        if any(part.startswith('.') for part in relative.parts):
            continue
        if not path.resolve().is_relative_to(root):
            raise ValueError('Cache file escapes configured cache root')
        raw = path.read_bytes()
        try:
            data = json.loads(raw)
            metadata = data.get('metadata', data)
            reference, sha = metadata.get('source_reference'), metadata.get('source_sha256')
            sha = sha or path.stem
            reference = reference or (source_references or {}).get(sha)
            if not isinstance(reference, str) or not reference.startswith(prefix):
                continue
            ReceiptMessage(sha, reference)  # Validate provenance before selecting a file.
        except (ValueError, TypeError, AttributeError):
            continue
        if files.get(reference) != sha:
            changes.append({'cache': str(path), 'relative_cache': relative.as_posix(),
                            'source_reference': reference, 'source_sha256': sha,
                            'cache_mtime_ns': path.stat().st_mtime_ns,
                            'cache_sha256': hashlib.sha256(raw).hexdigest()})
    return changes


def refresh(connect, prefix, *, scanned_root, electronic_root, cache_root, archive_root,
            actor, apply=False, expected_version=None):
    files = sources.inventory(prefix, scanned_root, electronic_root)
    database = sources.reconcile(connect, prefix, scanned_root=scanned_root,
        electronic_root=electronic_root, actor=actor)
    references = database.get('source_references',{})
    caches = stale_caches(cache_root, prefix, files, references)
    token = hashlib.sha256(json.dumps({'database_version': database['version'],
        'caches': caches}, sort_keys=True).encode()).hexdigest()
    result = {'apply': False, 'version': token, 'database': database,
              'stale_caches': caches, 'cache_retirements': [],
              'captured_at_utc': datetime.now(timezone.utc).isoformat()}
    if not apply:
        return result
    if expected_version != token:
        raise ValueError('Inputs changed or --expected-version is missing; preview refresh again')
    archive = Path(archive_root).expanduser().resolve()
    cache_root = Path(cache_root).expanduser().resolve()
    if archive == cache_root or archive.is_relative_to(cache_root):
        raise ValueError('Retained cache evidence must be outside the active OCR cache')
    with ExitStack() as locks:
        for sha in sorted({c['source_sha256'] for c in caches}):
            locks.enter_context(cache_rebuild_lock(cache_root, sha))
        if stale_caches(cache_root, prefix, files, references) != caches:
            raise ValueError('OCR cache changed; preview refresh again')
        retained = archive / str(uuid.uuid4())
        for cache in caches:
            source = Path(cache['cache'])
            target = retained / cache['relative_cache']
            target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            for directory in (archive, retained, *target.parents):
                if directory == archive or directory.is_relative_to(archive):
                    directory.chmod(0o700)
            raw = source.read_bytes()
            if hashlib.sha256(raw).hexdigest() != cache['cache_sha256']:
                raise ValueError('OCR cache changed while retaining evidence')
            target.write_bytes(raw)
            target.chmod(0o600)
            result['cache_retirements'].append({**cache, 'retained_path': str(target)})
        # Do not remove active cache bytes unless the DB reconciliation commits.
        result['database'] = sources.reconcile(connect, prefix, scanned_root=scanned_root,
            electronic_root=electronic_root, actor=actor, apply=True,
            expected_version=database['version'])
        for cache in result['cache_retirements']:
            source = Path(cache['cache'])
            if hashlib.sha256(source.read_bytes()).hexdigest() != cache['cache_sha256']:
                cache['status'] = 'changed_after_database_commit'
                continue
            current = sources.inventory(prefix, scanned_root, electronic_root)
            if current.get(cache['source_reference']) == cache['source_sha256']:
                cache['status'] = 'source_returned_cache_preserved'
                continue
            source.unlink()
            cache['status'] = 'retired'
        result['apply'] = True
        if caches:
            manifest = retained/'refresh-manifest.private.json'
            manifest.write_text(json.dumps(result, indent=2, default=str))
            manifest.chmod(0o600)
    return result


def run(args):
    import os
    from ..web.queries import LedgerQueryService
    result = refresh(LedgerQueryService()._connect, args.source_prefix,
        scanned_root=os.getenv('RECEIPT_SOURCE_ROOT',''),
        electronic_root=os.getenv('ELECTRONIC_RECEIPT_SOURCE_ROOT',''),
        cache_root=args.ocr_cache, archive_root=args.archive_root, actor=args.reviewer,
        apply=args.apply, expected_version=args.expected_version)
    print(json.dumps(result, default=str))
    return 0
