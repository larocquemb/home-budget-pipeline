import hashlib
import json

import pytest

from home_budget_pipeline.receipts import source_refresh as refresh


@pytest.fixture
def setup(tmp_path, monkeypatch):
    inputs=tmp_path/'inputs';(inputs/'batch').mkdir(parents=True)
    cache=tmp_path/'cache';(cache/'electronic/batch').mkdir(parents=True)
    path=cache/'electronic/batch/deleted.pdf.json'
    path.write_text(json.dumps({'metadata':{'source_reference':'electronic/batch/deleted.pdf',
                                           'source_sha256':hashlib.sha256(b'original').hexdigest()},
                                'plain_text':'original retained OCR'}))
    database_calls=[]
    def reconcile(*a,**kw):
        database_calls.append(kw)
        return {'version':'db-version','apply':kw.get('apply',False),'changes':[]}
    monkeypatch.setattr(refresh.sources,'reconcile',reconcile)
    kwargs=dict(scanned_root='',electronic_root=str(inputs),cache_root=cache,
                archive_root=tmp_path/'retained',actor='Test reviewer')
    return inputs,path,kwargs,database_calls


def test_refresh_retires_stale_cache_only_after_db_commit_and_retains_bytes(setup):
    inputs,path,kwargs,calls=setup
    original=path.read_bytes()
    preview=refresh.refresh(None,'electronic/batch/',**kwargs)
    assert path.read_bytes()==original and not preview['apply']
    result=refresh.refresh(None,'electronic/batch/',**kwargs,apply=True,expected_version=preview['version'])
    assert not path.exists()
    retirement,=result['cache_retirements']
    from pathlib import Path
    assert Path(retirement['retained_path']).read_bytes()==original
    assert retirement['status']=='retired'
    assert calls[-1]['apply'] is True


def test_refresh_preserves_cache_when_database_apply_fails(setup,monkeypatch):
    _,path,kwargs,_=setup
    preview=refresh.refresh(None,'electronic/batch/',**kwargs)
    original=path.read_bytes()
    def failure(*a,**kw):
        if kw.get('apply'):raise RuntimeError('DB rollback')
        return {'version':'db-version','changes':[]}
    monkeypatch.setattr(refresh.sources,'reconcile',failure)
    with pytest.raises(RuntimeError):
        refresh.refresh(None,'electronic/batch/',**kwargs,apply=True,expected_version=preview['version'])
    assert path.read_bytes()==original


def test_changed_cache_rejects_stale_preview_before_apply(setup):
    _,path,kwargs,calls=setup
    preview=refresh.refresh(None,'electronic/batch/',**kwargs)
    path.write_text(path.read_text().replace('original retained','changed retained'))
    with pytest.raises(ValueError,match='Inputs changed'):
        refresh.refresh(None,'electronic/batch/',**kwargs,apply=True,expected_version=preview['version'])
    assert path.exists() and not any(c.get('apply') for c in calls)


def test_live_source_keeps_cache_and_unavailable_folder_refuses_db_changes(setup):
    inputs,path,kwargs,calls=setup
    source=inputs/'batch/deleted.pdf';source.write_bytes(b'original')
    assert not refresh.refresh(None,'electronic/batch/',**kwargs)['stale_caches']
    source.unlink();source.parent.rmdir();before=len(calls)
    with pytest.raises(ValueError,match='unavailable'):
        refresh.refresh(None,'electronic/batch/',**kwargs)
    assert len(calls)==before and path.exists()


def test_legacy_hash_cache_uses_database_source_provenance(setup,monkeypatch):
    _,path,kwargs,_=setup
    sha=json.loads(path.read_text())['metadata']['source_sha256']
    from pathlib import Path
    legacy=Path(kwargs['cache_root'])/(sha+'.json')
    legacy.write_text(json.dumps({'source_sha256':sha,'page_text':['original OCR']}))
    path.unlink()
    monkeypatch.setattr(refresh.sources,'reconcile',lambda *a,**kw:{
        'version':'db-version','changes':[],
        'source_references':{sha:'electronic/batch/deleted.pdf'}})
    preview=refresh.refresh(None,'electronic/batch/',**kwargs)
    assert preview['stale_caches'][0]['cache']==str(legacy)
    result=refresh.refresh(None,'electronic/batch/',**kwargs,apply=True,expected_version=preview['version'])
    assert not legacy.exists() and result['cache_retirements'][0]['status']=='retired'
