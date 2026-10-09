import importlib.util
from pathlib import Path
import plistlib


def test_receipt_services_use_distinct_labels_logs_and_load_settings_at_runtime():
    spec = importlib.util.spec_from_file_location('receipt_services', 'scripts/receipt_services.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = Path('/Users/example/Development/home budget')
    labels, logs = set(), set()
    for name in ['worker', 'collector']:
        config = plistlib.loads(plistlib.dumps(module.service_plist(root, name)))
        labels.add(config['Label'])
        logs.add(config['StandardOutPath'])
        assert config['ProgramArguments'] == ['/bin/sh', str(root / 'scripts/run_receipt_service.sh'), name]
        assert config['WorkingDirectory'] == str(root)
        assert config['KeepAlive'] is True and config['RunAtLoad'] is True
        assert config['StandardErrorPath'] == config['StandardOutPath']
        assert set(config['EnvironmentVariables']) == {'PATH'}
    assert len(labels) == len(logs) == 2
    wrapper = Path('scripts/run_receipt_service.sh').read_text()
    assert '. "$repo_root/.env.dev"' in wrapper
    assert 'runtime.env' not in wrapper
    assert 'exec "$repo_root/.venv/bin/ledger" receipts "$mode"' in wrapper


def test_bootstrap_waits_for_previous_agent_shutdown(monkeypatch):
    import subprocess
    spec = importlib.util.spec_from_file_location('receipt_services', 'scripts/receipt_services.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    responses = iter([5, 5, 0])
    calls, waits = [], []
    def launchctl(*args, check=True):
        calls.append(args)
        return subprocess.CompletedProcess(args, next(responses), stderr='')
    monkeypatch.setattr(module, 'launchctl', launchctl)
    monkeypatch.setattr(module.time, 'sleep', waits.append)
    module.bootstrap_agent('gui/501', Path('/tmp/worker.plist'))
    assert len(calls) == 3
    assert waits == [0.25, 0.25]


def test_bootstrap_surfaces_permanent_failure_without_retry(monkeypatch):
    import subprocess
    import pytest
    spec = importlib.util.spec_from_file_location('receipt_services', 'scripts/receipt_services.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    def launchctl(*args, check=True):
        calls.append(args)
        return subprocess.CompletedProcess(args, 37, stderr='invalid configuration')
    monkeypatch.setattr(module, 'launchctl', launchctl)
    with pytest.raises(subprocess.CalledProcessError) as failure:
        module.bootstrap_agent('gui/501', Path('/tmp/worker.plist'))
    assert failure.value.returncode == 37
    assert len(calls) == 1


def test_worker_scaling_persists_settings_and_removes_extra_agents(tmp_path, monkeypatch):
    import sys
    import subprocess
    spec = importlib.util.spec_from_file_location('receipt_services', 'scripts/receipt_services.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root, home = tmp_path / 'repo', tmp_path / 'home'
    (root / '.venv/bin').mkdir(parents=True)
    (root / '.venv/bin/ledger').touch()
    (root / '.env.dev').touch()
    monkeypatch.setattr(module, 'ROOT', root)
    monkeypatch.setattr(Path, 'home', lambda: home)
    loaded = set()
    def launchctl(*args, check=True):
        command = args[0]
        code = 0
        if command == 'print':
            code = 0 if args[1].split('/')[-1] in loaded else 113
        elif command == 'bootstrap':
            loaded.add(plistlib.loads(Path(args[2]).read_bytes())['Label'])
        elif command == 'bootout':
            loaded.remove(args[1].split('/')[-1])
        return subprocess.CompletedProcess(args, code, stdout='', stderr='')
    monkeypatch.setattr(module, 'launchctl', launchctl)
    monkeypatch.setattr(sys, 'argv', ['receipt_services.py', 'start', '--workers', '3', '--ocr-threads', '4'])
    module.main()
    assert loaded == {module.service_label(n) for n in ('worker', 'worker-2', 'worker-3', 'collector')}
    agents = home / 'Library/LaunchAgents'
    config = plistlib.loads((agents / (module.service_label('worker-2') + '.plist')).read_bytes())
    assert config['ProgramArguments'][1:] == [str(root / 'scripts/run_receipt_service.sh'), 'worker-2']
    assert config['EnvironmentVariables']['HOME_BUDGET_SERVICE_OCR_THREADS'] == '4'
    assert config['StandardOutPath'].endswith('/worker-2.log')
    monkeypatch.setattr(sys, 'argv', ['receipt_services.py', 'restart'])
    module.main()
    assert len(loaded) == 4
    monkeypatch.setattr(sys, 'argv', ['receipt_services.py', 'start', '--workers', '1'])
    module.main()
    assert loaded == {module.service_label(n) for n in ('worker', 'collector')}
    assert module.installed_names(agents) == {'worker', 'collector'}
    monkeypatch.setattr(sys, 'argv', ['receipt_services.py', 'stop'])
    module.main()
    assert not loaded


def test_paddle_receives_explicit_thread_count_only_when_configured(monkeypatch):
    import sys
    import types
    from home_budget_pipeline.receipts import ingest
    options = []
    class Paddle:
        def __init__(self, **kwargs): options.append(kwargs)
        def predict(self, path):
            return [types.SimpleNamespace(json={'res': {'rec_texts': ['Milk'], 'rec_scores': [0.9]}})]
    monkeypatch.setitem(sys.modules, 'paddleocr', types.SimpleNamespace(PaddleOCR=Paddle))
    monkeypatch.setenv('HOME_BUDGET_PADDLE_OCR', 'true')
    image = types.SimpleNamespace(save=lambda path: None)
    for threads in (None, '4'):
        monkeypatch.setattr(ingest, '_PADDLE_OCR', None)
        if threads:
            monkeypatch.setenv('HOME_BUDGET_OCR_THREADS', threads)
        else:
            monkeypatch.delenv('HOME_BUDGET_OCR_THREADS', raising=False)
        candidate = ingest._run_paddle_candidate(image, dpi=300)
        assert candidate.text == 'Milk'
    assert 'cpu_threads' not in options[0]
    assert options[1]['cpu_threads'] == 4


def test_worker_wrapper_applies_saved_threads_after_loading_env(tmp_path):
    import os
    import json
    import subprocess
    root = tmp_path / 'repo'
    (root / 'scripts').mkdir(parents=True)
    wrapper = root / 'scripts/run_receipt_service.sh'
    wrapper.write_text(Path('scripts/run_receipt_service.sh').read_text())
    (root / '.env.dev').write_text('HOME_BUDGET_OCR_THREADS=9\n')
    (root / '.venv/bin').mkdir(parents=True)
    ledger = root / '.venv/bin/ledger'
    ledger.write_text('#!/usr/bin/env python3\nimport json,os,sys\nprint(json.dumps({"args":sys.argv[1:],"threads":os.environ.get("HOME_BUDGET_OCR_THREADS"),"omp":os.environ.get("OMP_THREAD_LIMIT"),"omp_num":os.environ.get("OMP_NUM_THREADS")}))\n')
    ledger.chmod(0o700)
    env = {**os.environ, 'HOME_BUDGET_SERVICE_OCR_THREADS': '4'}
    result = subprocess.run(['/bin/sh', str(wrapper), 'worker-2'], env=env, check=True, capture_output=True, text=True)
    assert json.loads(result.stdout) == {'args': ['receipts', 'consume'], 'threads': '4', 'omp': '4', 'omp_num': '1'}


def test_tesseract_threads_are_scoped_to_child_process(monkeypatch):
    import os
    import subprocess
    from home_budget_pipeline.receipts import ingest
    monkeypatch.setenv('HOME_BUDGET_OCR_THREADS', '4')
    monkeypatch.setenv('OMP_NUM_THREADS', '1')
    monkeypatch.setattr(ingest.shutil, 'which', lambda name: '/bin/tesseract')
    calls = []
    def run(*args, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout='', stderr='')
    monkeypatch.setattr(ingest.subprocess, 'run', run)
    ingest._run_tesseract(Path('receipt.png'))
    ingest._run_tesseract_candidate(Path('receipt.png'), dpi=300, psm='6')
    assert len(calls) == 2
    for call in calls:
        assert call['env']['OMP_NUM_THREADS'] == '4'
        assert call['env']['OMP_THREAD_LIMIT'] == '4'
    assert os.environ['OMP_NUM_THREADS'] == '1'
