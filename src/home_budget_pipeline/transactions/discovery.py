"""Discover stable CSV downloads, retain originals and retry failed imports."""
from datetime import datetime,timedelta,timezone
import hashlib
import json
import os
from pathlib import Path
from psycopg.types.json import Jsonb
from .tree_import import plans
from .workflows import ensure_schema,import_bank_csv


def source_root():
    from ..config import runtime_paths
    return Path(os.getenv('TRANSACTION_SOURCE_ROOT',str(runtime_paths().data_root/'transactions/raw'))).expanduser().resolve()


def retain(data,path):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    try:
        with path.open('xb') as out:
            os.chmod(path,0o600)
            out.write(data)
    except FileExistsError:
        if path.read_bytes()!=data:
            raise ValueError('Retained source hash does not match its contents')


def scan(conn,root,actor,*,settle_seconds=10,now=None,retained_root=None,retry_failed=False):
    now=now or datetime.now(timezone.utc)
    root=Path(root).resolve()
    if not root.is_dir():
        raise ValueError('Transaction archive is unavailable; no files processed')
    if settle_seconds<0:
        raise ValueError('Settle seconds must be nonnegative')
    retained_root=Path(retained_root or os.getenv('TRANSACTION_RETAINED_ROOT',str(root.parent/'retained'))).resolve()
    if retained_root.is_relative_to(root):
        raise ValueError('Retained originals must be outside the discovery inbox')
    results=[]
    with conn.transaction():
        locked=conn.execute('SELECT pg_try_advisory_xact_lock(hashtext(%s)) AS acquired',('bank-discovery:'+str(root),)).fetchone()['acquired']
        if not locked:
            return {'busy':True,'files':[],'new_transactions':0}
        if not conn.execute("SELECT to_regclass('accounting.transaction_discovery') AS present").fetchone()['present']:
            ensure_schema(conn)
        for file in sorted(root.rglob('*.csv')):
            uri=str(file)
            digest='';retained=None
            try:
                if file.is_symlink() or not file.resolve().is_relative_to(root):
                    raise ValueError('Source links outside the archive are not supported')
                stat=file.stat();data=file.read_bytes();after=file.stat()
                if (stat.st_size,stat.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
                    results.append({'file':uri,'status':'waiting','reason':'file is changing'})
                    continue
                digest=hashlib.sha256(data).hexdigest()
                old=conn.execute('SELECT * FROM accounting.transaction_discovery WHERE source_uri=%s FOR UPDATE',(uri,)).fetchone()
                same=old and old['source_sha256']==digest and old['source_size']==stat.st_size and old['source_mtime_ns']==stat.st_mtime_ns
                observed=old['observed_at'] if same else now
                conn.execute('''INSERT INTO accounting.transaction_discovery
                    (source_uri,root_uri,source_sha256,source_size,source_mtime_ns,status,observed_at,checked_at)
                    VALUES(%s,%s,%s,%s,%s,'waiting',%s,%s) ON CONFLICT(source_uri) DO UPDATE SET
                    root_uri=EXCLUDED.root_uri,source_sha256=EXCLUDED.source_sha256,source_size=EXCLUDED.source_size,
                    source_mtime_ns=EXCLUDED.source_mtime_ns,observed_at=EXCLUDED.observed_at,checked_at=EXCLUDED.checked_at,
                    attempt_count=CASE WHEN accounting.transaction_discovery.source_sha256=EXCLUDED.source_sha256
                        THEN accounting.transaction_discovery.attempt_count ELSE 0 END,
                    retry_after=CASE WHEN accounting.transaction_discovery.source_sha256=EXCLUDED.source_sha256
                        THEN accounting.transaction_discovery.retry_after ELSE NULL END''',
                    (uri,str(root),digest,stat.st_size,stat.st_mtime_ns,observed,now))
                known=conn.execute('''SELECT import_id FROM accounting.source_artifacts
                    WHERE source_uri=%s AND source_sha256=%s''',(uri,digest)).fetchone()
                if known or (same and old['status']=='imported'):
                    retained=retained_root/digest[:2]/(digest+'.csv')
                    retain(data,retained)
                    if known:
                        conn.execute('''INSERT INTO accounting.source_artifacts(import_id,source_uri,source_sha256)
                            VALUES(%s,%s,%s) ON CONFLICT DO NOTHING''',(known['import_id'],str(retained),digest))
                    conn.execute("UPDATE accounting.transaction_discovery SET status='imported',error_summary=NULL,retry_after=NULL WHERE source_uri=%s",(uri,))
                    results.append({'file':uri,'status':'unchanged'})
                    continue
                if not same or (now-observed).total_seconds()<settle_seconds or now.timestamp()-stat.st_mtime<settle_seconds:
                    conn.execute("UPDATE accounting.transaction_discovery SET status='waiting',error_summary=NULL WHERE source_uri=%s",(uri,))
                    results.append({'file':uri,'status':'waiting'})
                    continue
                if old['retry_after'] and now<old['retry_after'] and not retry_failed:
                    results.append({'file':uri,'status':'retry_wait'})
                    continue
                retained=retained_root/digest[:2]/(digest+'.csv')
                retain(data,retained)
                with conn.transaction():
                    plan=plans(conn,root,files=[file],contents={file:data})[0]
                    _,reference,kind,ending,currency,institution=plan
                    if not conn.execute('SELECT 1 FROM accounting.accounts WHERE account_reference=%s',(reference,)).fetchone():
                        raise ValueError('Account profile/currency is not configured for automatic import')
                    # Use the captured stable bytes even if a download is replaced.
                    report=import_bank_csv(conn,file,reference,actor,apply=True,account_type=kind,
                        account_last4=ending,currency=currency,institution=institution,source_data=data)
                    conn.execute('''INSERT INTO accounting.source_artifacts(import_id,source_uri,source_sha256)
                        VALUES(%s,%s,%s) ON CONFLICT DO NOTHING''',(report['import_id'],str(retained),digest))
                    conn.execute("UPDATE accounting.transaction_discovery SET status='imported',imported_at=%s,attempt_count=attempt_count+1,retry_after=NULL,error_summary=NULL WHERE source_uri=%s",(now,uri))
                    conn.execute('''INSERT INTO accounting.transaction_discovery_attempts
                        (source_uri,source_sha256,retained_uri,status,actor,result)
                        VALUES(%s,%s,%s,'imported',%s,%s)''',(uri,digest,str(retained),actor,Jsonb(report)))
                results.append({'file':uri,'status':'imported','new':report['new'],'already_imported':report['already_imported']})
            except Exception as exc:
                from psycopg import Error
                from psycopg.pq import TransactionStatus
                if isinstance(exc,Error) and conn.info.transaction_status in (TransactionStatus.INERROR,TransactionStatus.UNKNOWN):
                    raise
                # Savepoints isolate a malformed download; other files continue.
                error=str(exc) if isinstance(exc,ValueError) else type(exc).__name__
                if not digest:
                    conn.execute('''INSERT INTO accounting.transaction_discovery
                        (source_uri,root_uri,source_sha256,source_size,source_mtime_ns,status,observed_at,checked_at,error_summary)
                        VALUES(%s,%s,'',0,0,'failed',%s,%s,%s) ON CONFLICT(source_uri) DO UPDATE SET
                        status='failed',checked_at=EXCLUDED.checked_at,error_summary=EXCLUDED.error_summary''',
                        (uri,str(root),now,now,error))
                    results.append({'file':uri,'status':'failed','error':error})
                    continue
                attempts=(old['attempt_count'] if same else 0)+1
                retry=now+timedelta(seconds=min(3600,60*2**min(attempts-1,6)))
                conn.execute("UPDATE accounting.transaction_discovery SET status='failed',attempt_count=%s,retry_after=%s,error_summary=%s WHERE source_uri=%s",(attempts,retry,error,uri))
                conn.execute('''INSERT INTO accounting.transaction_discovery_attempts
                    (source_uri,source_sha256,retained_uri,status,actor,result) VALUES(%s,%s,%s,'failed',%s,%s)''',
                    (uri,digest,str(retained) if retained else None,actor,Jsonb({'error':error})))
                results.append({'file':uri,'status':'failed','error':error})
    return {'busy':False,'files':results,'new_transactions':sum(r.get('new',0) for r in results),
            'failed':sum(r['status']=='failed' for r in results),'waiting':sum(r['status']=='waiting' for r in results),
            'unchanged':sum(r['status']=='unchanged' for r in results)}


def status(conn):
    return conn.execute('''SELECT source_uri,root_uri,status,checked_at,imported_at,attempt_count,retry_after,error_summary
        FROM accounting.transaction_discovery ORDER BY source_uri''').fetchall()
