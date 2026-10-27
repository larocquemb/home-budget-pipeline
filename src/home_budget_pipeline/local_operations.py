"""Native local cache maintenance and repository service controls."""
import datetime
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import unquote, urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]


def validate_local(values, root):
    from .development_backend import HOSTS, validate_database_url
    validate_database_url(values.get('DATABASE_URL', ''))
    broker = urlsplit(values.get('RABBITMQ_URL', ''))
    if not (broker.scheme == 'amqp' and broker.hostname in HOSTS
            and broker.port == 15673 and unquote(broker.path) == '/home-budget-dev'
            and unquote(broker.username or '') == 'home_budget_dev' and broker.password
            and not broker.query and not broker.fragment):
        raise ValueError('Development cache maintenance requires isolated Arsene development RabbitMQ')
    configured = values.get('HOME_BUDGET_OCR_CACHE', '')
    if not configured:
        raise ValueError('Set HOME_BUDGET_OCR_CACHE in .env.dev')
    cache = Path(configured).expanduser()
    if not cache.is_absolute():
        cache = root / cache
    if cache.is_symlink() or cache.resolve() not in {root / '.ocr_cache', root / 'ocr-cache'}:
        raise ValueError('Local cache must be the repository .ocr_cache or ocr-cache directory, without a symlink')
    if cache.exists() and not cache.is_dir():
        raise ValueError('Local cache path must be a directory')
    return cache


def clean_cache(cache, root):
    """Clear the active cache by retaining the original outside tracked source."""
    backups = root / '.validation' / 'cache-backups'
    if not backups.resolve().is_relative_to(root):
        raise ValueError('Cache backup directory must stay inside the repository')
    backups.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(backups, 0o700)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    destination = backups / f'{stamp}-{uuid4().hex[:8]}'
    destination.mkdir(mode=0o700)
    if cache.exists():
        cache.rename(destination / 'cache')
    cache.mkdir(mode=0o700)
    artifacts = []
    retained = destination / 'cache'
    for path in sorted(retained.rglob('*')):
        if path.is_file() and not path.is_symlink():
            artifacts.append({
                'path': str(path.relative_to(destination)),
                'source_timestamp_ns': path.stat().st_mtime_ns,
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            })
    manifest = destination / 'manifest.private.json'
    manifest.write_text(json.dumps({
        'captured_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'source_path': str(cache), 'retained_path': str(retained) if retained.exists() else None,
        'activity': 'routine_development_operations', 'artifacts': artifacts,
        'backup_status': 'Local retained copy; independent backup unverified',
    }, indent=2) + '\n')
    manifest.chmod(0o600)
    print(f'Empty local cache: {cache}', flush=True)
    print(f'Previous cache/evidence retained: {destination}', flush=True)
    print('Receipt services remain stopped. Database and queued jobs are unchanged.', flush=True)


def _env_path(args):
    return Path(args.env_file).resolve() if args.env_file else ROOT / '.env.dev'


def run_receipt_services(args):
    # LaunchAgents load this specific file on startup; do not imply another file applies.
    if _env_path(args) != ROOT / '.env.dev':
        raise ValueError('Native receipt services use the repository .env.dev; select that file')
    command = [sys.executable, str(ROOT / 'scripts/receipt_services.py'), args.action]
    for name in ('workers', 'ocr_threads'):
        value = getattr(args, name, None)
        if value is not None:
            command.extend(['--' + name.replace('_', '-'), str(value)])
    return subprocess.run(command, cwd=ROOT).returncode


def _stop_services():
    pids = []
    agents = Path.home() / 'Library/LaunchAgents'
    paths = [*agents.glob('com.brownrook.home-budget.worker*.plist'),
             agents / 'com.brownrook.home-budget.collector.plist']
    for path in paths:
        result = subprocess.run(['launchctl', 'print', f'gui/{os.getuid()}/{path.stem}'],
                                capture_output=True, text=True)
        match = re.search(r'^\s*pid = (\d+)\s*$', result.stdout, re.MULTILINE)
        if match:
            pids.append(int(match[1]))
    subprocess.run([sys.executable, str(ROOT / 'scripts/receipt_services.py'), 'stop'],
                   cwd=ROOT, check=True)
    deadline = time.monotonic() + 15
    while pids:
        remaining = []
        for pid in pids:
            try:
                os.kill(pid, 0)
                remaining.append(pid)
            except ProcessLookupError:
                pass
        pids = remaining
        if pids and time.monotonic() >= deadline:
            raise ValueError('Receipt processes have not exited; refusing to change cache or database')
        if pids:
            time.sleep(0.1)


def clean_local_cache(args):
    from .cli_environment import read_environment
    if _env_path(args) != ROOT / '.env.dev':
        raise ValueError('Local cache cleaning requires the repository .env.dev')
    cache = validate_local(read_environment(_env_path(args)), ROOT)
    _stop_services()
    clean_cache(cache, ROOT)
    return 0


def rebuild_database(args):
    command = [sys.executable, str(ROOT / 'scripts/development_backend.py'), 'reset',
               '--env-file', str(_env_path(args)), '--confirm', args.confirm]
    if args.target == 'arsene-test':
        command.append('--test')
    return subprocess.run(command, cwd=ROOT).returncode


def development_backend(args):
    if args.apply and args.action != 'configure':
        raise ValueError('--apply is only valid with development configure')
    if args.test and args.action != 'bootstrap':
        raise ValueError('--test is only valid with development bootstrap')
    command = [sys.executable, str(ROOT / 'scripts/development_backend.py'), args.action,
               '--env-file', str(_env_path(args))]
    if args.apply:
        command.append('--apply')
    if args.test:
        command.append('--test')
    if args.secrets_file:
        command.extend(['--secrets-file', args.secrets_file])
    return subprocess.run(command, cwd=ROOT).returncode
