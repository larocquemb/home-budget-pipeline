"""Infrastructure-only, no-backup production schema rebuild through Kubernetes."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ('ingest_blue','ingest_green','ingest','ops','enrichment','lineage','budget')
SKIP = 'argocd.argoproj.io/skip-reconcile'
PAUSE = ('autoscaling.keda.sh/paused','autoscaling.keda.sh/paused-replicas')


def atomic_bootstrap(files):
    """Remove file-level transaction wrappers, keeping one atomic reset."""
    chunks = []
    for path in files:
        lines = path.read_text().splitlines()
        statements = [i for i,line in enumerate(lines) if line.strip() and not line.lstrip().startswith('--')]
        if statements and lines[statements[0]].strip().upper() == 'BEGIN;':
            if lines[statements[-1]].strip().upper() != 'COMMIT;':
                raise ValueError('Bootstrap transaction wrapper is incomplete')
            lines[statements[0]] = lines[statements[-1]] = ''
        text = '\n'.join(lines)
        if re.search(r'^(?:BEGIN|COMMIT|ROLLBACK);\s*$',text,re.I|re.M):
            raise ValueError('Unexpected nested bootstrap transaction')
        chunks.append(text)
    checks = """DO $verify$ BEGIN
      IF to_regclass('budget.analytics_expenses') IS NULL
         OR to_regclass('ingest.receipts') IS NULL
         OR to_regclass('budget.ocr_result_events') IS NULL
         OR NOT EXISTS (SELECT 1 FROM budget.expense_categories)
      THEN RAISE EXCEPTION 'Production bootstrap verification failed'; END IF;
    END $verify$;"""
    return ('BEGIN;\nSET LOCAL lock_timeout=\'15s\';\nSET LOCAL statement_timeout=\'180s\';\n'
            + ''.join(f'DROP SCHEMA IF EXISTS {name} CASCADE;\n' for name in SCHEMAS)
            + '\n'.join(chunks) + '\n' + checks + '\nCOMMIT;\n')


class Kubernetes:
    def __init__(self, context, directory):
        self.context,self.directory = context,directory

    def command(self,*args,namespace='home-budget'):
        return ['kubectl','--context',self.context,'-n',namespace,*args]

    def call(self,*args,namespace='home-budget',data=None):
        result = subprocess.run(self.command(*args,namespace=namespace),input=data,
                                stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        if result.returncode:
            with (self.directory/'errors.private.log').open('ab') as log:
                log.write(result.stderr)
            raise RuntimeError(f'Production maintenance failed; see {self.directory}/errors.private.log')
        return result.stdout

    def get(self,kind,name=None,namespace='home-budget'):
        return json.loads(self.call('get',kind,*([name] if name else []),'-o','json',namespace=namespace))

    def patch(self,kind,name,value,namespace='home-budget'):
        self.call('patch',kind,name,'--type=merge','-p',json.dumps(value),namespace=namespace)

    def sql(self,text):
        shell = ('set -eu; test "$POSTGRES_DB" = home_budget; '
                 'export PGPASSWORD="${POSTGRES_PASSWORD:-}"; '
                 'exec psql -X -U "$POSTGRES_USER" -d home_budget -v ON_ERROR_STOP=1 -At')
        result = self.call('exec','-i','postgres-0','-c','postgres','--','sh','-c',shell,data=text.encode())
        log = self.directory/'sql-output.private.log'
        with log.open('ab') as stream:
            stream.write(result+b'\n')
        log.chmod(0o600)
        return result


def snapshot(kube):
    identity = json.loads(kube.sql("SELECT json_build_object('database',current_database(),"
        "'environment',COALESCE(current_setting('home_budget.environment',true),''));"))
    if identity['database'] != 'home_budget' or identity['environment'] not in ('','production'):
        raise ValueError('Refusing an unexpected production database identity')
    app = kube.get('application','ledger',namespace='argocd')
    if app.get('operation') or app.get('status',{}).get('operationState',{}).get('phase') in ('Running','Terminating'):
        raise ValueError('Wait for the active Argo operation to finish before rebuilding')
    deployments = kube.get('deployments')['items']
    names = {d['metadata']['name'] for d in deployments}
    scaled = [s for s in kube.get('scaledobjects.keda.sh')['items'] if s['spec']['scaleTargetRef']['name'] in names]
    targets = {s['spec']['scaleTargetRef']['name'] for s in scaled}
    for hpa in kube.get('hpa')['items']:
        if hpa['spec']['scaleTargetRef']['name'] in names and hpa['spec']['scaleTargetRef']['name'] not in targets:
            raise ValueError('A non-KEDA autoscaler must be paused before this rebuild')
    result = {'context':kube.context,'database':'home_budget','database_backup':False,
              'argo_automated':app['spec'].get('syncPolicy',{}).get('automated'),
              'argo_skip':app['metadata'].get('annotations',{}).get(SKIP),
              'deployments':[], 'scaled_objects':[], 'cronjobs':[]}
    for d in deployments:
        selector = d['spec']['selector']
        if not selector.get('matchLabels') or selector.get('matchExpressions'):
            raise ValueError('Unsupported deployment selector; refusing incomplete writer shutdown')
        result['deployments'].append({'name':d['metadata']['name'],'replicas':d['spec'].get('replicas',1),
            'selector':','.join(f'{k}={v}' for k,v in sorted(selector['matchLabels'].items()))})
    for s in scaled:
        result['scaled_objects'].append({'name':s['metadata']['name'],
            'annotations':{key:s['metadata'].get('annotations',{}).get(key) for key in PAUSE}})
    for c in kube.get('cronjobs')['items']:
        result['cronjobs'].append({'name':c['metadata']['name'],'suspend':c['spec'].get('suspend')})
    return result


def resume_commands(kube,state):
    commands = []
    for d in state['deployments']:
        commands.append(kube.command('scale','deployment/'+d['name'],'--replicas='+str(d['replicas'])))
    for s in state['scaled_objects']:
        commands.append(kube.command('patch','scaledobject',s['name'],'--type=merge','-p',json.dumps({'metadata':{'annotations':s['annotations']}})))
    for c in state['cronjobs']:
        commands.append(kube.command('patch','cronjob',c['name'],'--type=merge','-p',json.dumps({'spec':{'suspend':c['suspend']}})))
    commands.append(kube.command('patch','application','ledger','--type=merge','-p',json.dumps({
        'metadata':{'annotations':{SKIP:state['argo_skip']}},
        'spec':{'syncPolicy':{'automated':state['argo_automated']}}}),namespace='argocd'))
    return commands


def execute(kube,state,sql):
    complete = False
    try:
        kube.patch('application','ledger',{'metadata':{'annotations':{SKIP:'true'}},
            'spec':{'syncPolicy':{'automated':None}}},namespace='argocd')
        app = kube.get('application','ledger',namespace='argocd')
        if app.get('operation') or app.get('status',{}).get('operationState',{}).get('phase') in ('Running','Terminating'):
            raise ValueError('An Argo sync started during maintenance preparation; retry after it finishes')
        for c in state['cronjobs']:
            kube.patch('cronjob',c['name'],{'spec':{'suspend':True}})
        if any(job.get('status',{}).get('active',0) for job in kube.get('jobs')['items']):
            raise ValueError('Active production Jobs remain; let them finish and rerun')
        for s in state['scaled_objects']:
            kube.patch('scaledobject',s['name'],{'metadata':{'annotations':{PAUSE[0]:'true',PAUSE[1]:'0'}}})
            kube.call('wait','--for=condition=Paused','scaledobject/'+s['name'],'--timeout=60s')
        for d in state['deployments']:
            kube.call('scale','deployment/'+d['name'],'--replicas=0')
        for d in state['deployments']:
            kube.call('wait','--for=delete','pod','-l',d['selector'],'--timeout=180s')
        # All database clients are stopped. A failed transaction rolls back the
        # drops and bootstrap together; no database dump/backup is created.
        kube.sql(sql)
        complete = True
    finally:
        healthy = complete
        if not healthy:
            try:
                healthy = kube.sql("SELECT to_regclass('budget.analytics_expenses') IS NOT NULL;").strip() == b't'
            except Exception:
                pass
        if healthy:
            for command in resume_commands(kube,state):
                result = subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                if result.returncode:
                    raise RuntimeError(f'Restore workload settings using {kube.directory}/resume.sh')
        else:
            print(f'Workloads remain paused. Check the database, then use {kube.directory}/resume.sh',flush=True)


def run(args):
    if not args.plan and args.confirm != 'home_budget':
        raise ValueError('Production rebuild requires --confirm home_budget; it creates no database backup')
    directory = ROOT/'.validation/production-rebuild'/(
        datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8])
    directory.mkdir(parents=True,mode=0o700);directory.chmod(0o700)
    kube = Kubernetes(args.kube_context,directory)
    state = snapshot(kube)
    env = {**os.environ,'PATH':str(Path(sys.executable).parent)+os.pathsep+os.environ.get('PATH','')}
    subprocess.run([str(ROOT/'scripts/stage_db_bootstrap.sh'),str(directory/'bootstrap')],env=env,check=True,
                   stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    files = sorted((directory/'bootstrap').glob('*.sql'))
    if len(files) != 11:
        raise ValueError('Expected the complete 11-file bootstrap sequence')
    sql = atomic_bootstrap(files)
    state['bootstrap'] = [{'file':p.name,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in files]
    (directory/'state.private.json').write_text(json.dumps(state,indent=2))
    (directory/'rebuild.sql').write_text(sql)
    (directory/'resume.sh').write_text('#!/bin/sh\nset -eu\n'+ '\n'.join(shlex.join(c) for c in resume_commands(kube,state))+'\n')
    for p in directory.rglob('*'):
        p.chmod(0o700 if p.is_dir() else 0o600)
    print(f'Production: {args.kube_context}/home-budget, database home_budget; no database backup. Plan: {directory}',flush=True)
    if args.plan:
        print(json.dumps(state,indent=2))
        return 0
    print('Pausing controllers and database writers; rebuilding application schemas.',flush=True)
    try:
        execute(kube,state,sql)
        state['result'] = 'completed'
    except Exception as exc:
        state['result'] = 'failed'
        state['error_type'] = type(exc).__name__
        raise
    finally:
        state['finished_at_utc'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        (directory/'state.private.json').write_text(json.dumps(state,indent=2))
    print('Production schemas rebuilt and workload settings restored. Source files, OCR cache and RabbitMQ queues are unchanged.',flush=True)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan',action='store_true')
    parser.add_argument('--confirm',default=os.getenv('PRODUCTION_DB_CONFIRM',''))
    parser.add_argument('--kube-context',default=os.getenv('KUBE_CONTEXT','brownrook-k3s1'))
    args = parser.parse_args(argv)
    # Make's default invocation is a read-only plan. Exact database-name
    # confirmation is required to perform the infrastructure operation.
    if not args.confirm:
        args.plan = True
    return run(args)


if __name__ == '__main__':
    raise SystemExit(main())
