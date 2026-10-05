import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize("compare,collaborate,limit", [("0", "0", "100"), ("1", "0", "10"), ("0", "1", "1")])
def test_dry_run_clones_live_image_and_secrets_and_escapes_receipt(tmp_path, compare, collaborate, limit):
    kubectl = shutil.which("kubectl")
    if not kubectl:
        pytest.skip("kubectl is not installed")
    shim = tmp_path / "kubectl"
    shim.write_text('''#!/usr/bin/env python3
import json, os, subprocess, sys
args=sys.argv[1:]
if args[0] in ('patch', 'set'):
    raise SystemExit(subprocess.call([os.environ['REAL_KUBECTL'], *args]))
assert args[:4] == ['--context', 'test-context', '--namespace', 'test-namespace']
if '--from=cronjob/product-enrichment' in args:
    print(json.dumps({'apiVersion':'batch/v1','kind':'Job','metadata':{'name':'test-job'},
      'spec':{'template':{'spec':{'restartPolicy':'Never','containers':[{'name':'enrich',
       'image':'example@sha256:immutable','args':['--write-db'],
       'env':[{'name':'OPENAI_API_KEY','valueFrom':{'secretKeyRef':{'name':'openai-api','key':'OPENAI_API_KEY'}}}]}]}}}}))
else:
    assert '--dry-run=server' in args
    data=json.load(open(args[args.index('-f')+1]))
    json.dump(data,open(os.environ['CAPTURE_JOB'],'w'))
    print('job.batch/test-job')
''')
    shim.chmod(0o755)
    receipt = '2026-08-14/receipt "quoted" \\ café.pdf'
    capture = tmp_path / "captured.json"
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}",
           "REAL_KUBECTL": kubectl, "CAPTURE_JOB": str(capture),
           "COMPARE": compare, "COLLABORATE": collaborate, "LIMIT": "", "RECEIPT": receipt, "DRY_RUN": "1",
           "CONTEXT_TOKENS": "16384", "OUTPUT_TOKENS": "4096", "QWEN_MODELS": "qwen3:30b,qwen3-vl:8b"}
    script = Path(__file__).resolve().parents[1] / "scripts/enrichment_job.sh"
    subprocess.run(["bash", str(script), "test-context", "test-namespace", "test-job"],
                   env=env, check=True, capture_output=True, text=True)
    container = json.loads(capture.read_text())["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "example@sha256:immutable"
    assert container["env"][0]["valueFrom"]["secretKeyRef"]["name"] == "openai-api"
    args = container["args"]
    assert args[args.index("--receipt") + 1] == receipt
    assert args[args.index("--limit") + 1] == limit
    assert ("--compare-models" in args) == (compare == "1")
    assert ("--collaborate-models" in args) == (collaborate == "1")
    values = {entry['name']: entry.get('value') for entry in container['env']}
    assert values['COLLAB_CONTEXT_TOKENS'] == '16384'
    assert values['COLLAB_OUTPUT_TOKENS'] == '4096'
    assert values['OLLAMA_COLLAB_MODELS'] == 'qwen3:30b,qwen3-vl:8b'
