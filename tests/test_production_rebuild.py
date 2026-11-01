from argparse import Namespace
from types import SimpleNamespace

import pytest

from home_budget_pipeline import production_rebuild as prod
from home_budget_pipeline import local_operations,cli


def test_production_requires_exact_confirmation_before_kubernetes(monkeypatch):
    monkeypatch.setattr(prod,'Kubernetes',lambda *a:pytest.fail('accessed Kubernetes'))
    with pytest.raises(ValueError,match='--confirm home_budget'):
        prod.run(Namespace(plan=False,confirm='home_budget_dev'))


def test_production_dispatch_does_not_use_arsene_reset_script(monkeypatch):
    called=[]
    monkeypatch.setattr(prod,'run',lambda args:called.append(args) or 0)
    args=Namespace(target='production')
    assert local_operations.rebuild_database(args)==0 and called==[args]
    parsed=cli.build_parser().parse_args(['database','rebuild','--target','production','--plan'])
    assert parsed.plan and parsed.kube_context=='brownrook-k3s1'


def test_bootstrap_is_one_transaction_and_preserves_function_begin(tmp_path):
    path=tmp_path/'schema.sql'
    path.write_text('-- bootstrap\nBEGIN;\nCREATE FUNCTION f() RETURNS void AS $$\nBEGIN\nRETURN;\nEND\n$$ LANGUAGE plpgsql;\nCOMMIT;')
    sql=prod.atomic_bootstrap([path])
    assert sql.count('BEGIN;')==1 and sql.count('COMMIT;')==1
    assert '\nBEGIN\nRETURN;' in sql
    assert sql.index('DROP SCHEMA')<sql.index('CREATE FUNCTION')<sql.index('COMMIT;')
    path.write_text('BEGIN;\nSELECT 1;')
    with pytest.raises(ValueError):prod.atomic_bootstrap([path])


STATE={'deployments':[{'name':'writer','replicas':2,'selector':'app=writer'}],
       'cronjobs':[{'name':'importer','suspend':None}],
       'scaled_objects':[{'name':'writer','annotations':{prod.PAUSE[0]:None,prod.PAUSE[1]:None}}],
       'argo_automated':{'selfHeal':True},'argo_skip':None}


class FakeKube:
    directory='private-state'
    def __init__(self,active=False):self.events=[];self.active=active
    def patch(self,*args,**kw):self.events.append(('patch',args))
    def get(self,kind,*a,**kw):
        if kind=='application':return {}
        return {'items':[{'status':{'active':1}}] if self.active else []}
    def call(self,*args):self.events.append(('call',args))
    def sql(self,text):self.events.append(('sql',text));return b't'
    def command(self,*args,**kw):return ['kubectl',*args]


def test_writers_and_controllers_stop_before_reset_and_settings_restore(monkeypatch):
    kube=FakeKube();restores=[]
    monkeypatch.setattr(prod.subprocess,'run',lambda cmd,**kw:restores.append(cmd) or SimpleNamespace(returncode=0))
    prod.execute(kube,STATE,'RESET TRANSACTION')
    reset=kube.events.index(('sql','RESET TRANSACTION'))
    assert ('call',('wait','--for=delete','pod','-l','app=writer','--timeout=180s')) in kube.events[:reset]
    assert any(event[0]=='patch' and event[1][0]=='application' for event in kube.events[:reset])
    assert any('--replicas=2' in command for command in restores)
    assert '"automated": {"selfHeal": true}' in restores[-1][-1]


def test_active_jobs_abort_before_reset_and_restore_unchanged_database(monkeypatch):
    kube=FakeKube(active=True);restores=[]
    monkeypatch.setattr(prod.subprocess,'run',lambda cmd,**kw:restores.append(cmd) or SimpleNamespace(returncode=0))
    with pytest.raises(ValueError,match='Active production Jobs'):
        prod.execute(kube,STATE,'RESET TRANSACTION')
    assert ('sql','RESET TRANSACTION') not in kube.events
    assert restores


def test_snapshot_rejects_development_database():
    kube=SimpleNamespace(sql=lambda text:b'{"database":"home_budget_dev","environment":"development"}')
    with pytest.raises(ValueError,match='unexpected production'):
        prod.snapshot(kube)


def test_development_plan_flag_cannot_accidentally_run_a_reset(monkeypatch):
    monkeypatch.setattr(local_operations.subprocess,'run',lambda *a,**kw:pytest.fail('executed reset'))
    with pytest.raises(ValueError,match='no reset was run'):
        local_operations.rebuild_database(Namespace(target='arsene-dev',plan=True))
