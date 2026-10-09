#!/usr/bin/env python3
"""Manage per-user macOS receipt worker and collector LaunchAgents."""
from __future__ import annotations

import argparse
import os
import json
from pathlib import Path
import plistlib
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
NAMES = None


def is_worker(name: str) -> bool:
    return name == 'worker' or (name.startswith('worker-') and name[7:].isdigit())


def configured_names(workers: int) -> tuple[str, ...]:
    return ('worker', *(f'worker-{i}' for i in range(2, workers + 1)), 'collector')


def installed_names(agents: Path) -> set[str]:
    prefix = 'com.brownrook.home-budget.'
    return {p.stem[len(prefix):] for p in agents.glob(prefix + '*.plist')
            if is_worker(p.stem[len(prefix):]) or p.stem[len(prefix):] == 'collector'}


def positive_count(value: str) -> int:
    count = int(value)
    if not 1 <= count <= 64:
        raise argparse.ArgumentTypeError('Choose a count between 1 and 64')
    return count


def service_label(name: str) -> str:
    return f'com.brownrook.home-budget.{name}'


def service_plist(root: Path, name: str, threads: int | None = None) -> dict:
    config = {
        'Label': service_label(name),
        'ProgramArguments': ['/bin/sh', str(root / 'scripts' / (
            'run_receipt_service.sh' if is_worker(name) or name == 'collector' else 'run_local_telemetry.sh')), name],
        'WorkingDirectory': str(root),
        'RunAtLoad': True,
        'KeepAlive': True,
        'ThrottleInterval': 10,
        'ProcessType': 'Background',
        'EnvironmentVariables': {
            'PATH': '/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin',
        },
        'StandardOutPath': str(root / '.local-services' / f'{name}.log'),
        'StandardErrorPath': str(root / '.local-services' / f'{name}.log'),
    }

    if is_worker(name) and threads is not None:
        config['EnvironmentVariables']['HOME_BUDGET_SERVICE_OCR_THREADS'] = str(threads)
    return config


def launchctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(['launchctl', *args], check=check, capture_output=True, text=True)


def bootstrap_agent(domain: str, path: Path) -> None:
    # bootout returns before the prior process has finished shutting down.
    for _ in range(20):
        result = launchctl('bootstrap', domain, str(path), check=False)
        if result.returncode == 0:
            return
        if result.returncode != 5:
            break
        time.sleep(0.25)
    raise subprocess.CalledProcessError(result.returncode, result.args, stderr=result.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start', 'stop', 'restart', 'status', 'logs'))
    parser.add_argument('--workers', type=positive_count, help='Number of receipt consumers; saved for subsequent commands')
    parser.add_argument('--ocr-threads', type=positive_count, help='CPU threads per OCR engine in each worker')
    args = parser.parse_args()
    if (args.workers is not None or args.ocr_threads is not None) and args.action not in {'start', 'restart'}:
        parser.error('Worker settings apply to start or restart only')
    settings_path = ROOT / '.local-services/receipt-services.json'
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {'workers': 1, 'ocr_threads': None}
    if args.workers is not None:
        settings['workers'] = args.workers
    if args.ocr_threads is not None:
        settings['ocr_threads'] = args.ocr_threads
    names = NAMES if NAMES is not None else configured_names(settings['workers'])
    domain = f'gui/{os.getuid()}'
    agents = Path.home() / 'Library/LaunchAgents'
    if NAMES is None and args.action in {'stop', 'status', 'logs'}:
        names = tuple(dict.fromkeys((*names, *sorted(installed_names(agents)))))
    if args.action == 'logs':
        subprocess.run(['tail', '-n', '30', '-F', *(str(ROOT / '.local-services' / f'{n}.log') for n in names)])
        return 0
    if args.action in {'start', 'restart'}:
        if not (ROOT / '.env.dev').is_file() or not (ROOT / '.venv/bin/ledger').is_file():
            parser.error('Configure .env.dev and install the project in .venv first')
        agents.mkdir(parents=True, exist_ok=True)
        logs = ROOT / '.local-services'
        logs.mkdir(mode=0o700, exist_ok=True)
        if NAMES is None:
            settings_path.write_text(json.dumps(settings) + '\n')
            settings_path.chmod(0o600)
            for stale in installed_names(agents) - set(names):
                target = f'{domain}/{service_label(stale)}'
                if launchctl('print', target, check=False).returncode == 0:
                    launchctl('bootout', target)
                launchctl('disable', target)
                (agents / f'{service_label(stale)}.plist').unlink()
                print(f'{stale}: removed after reducing worker count')
        for name in names:
            (logs / f'{name}.log').touch(mode=0o600, exist_ok=True)
    for name in names:
        label = service_label(name)
        target = f'{domain}/{label}'
        path = agents / f'{label}.plist'
        loaded = launchctl('print', target, check=False).returncode == 0
        if args.action == 'stop':
            if loaded:
                launchctl('bootout', target)
            launchctl('disable', target)
            print(f'{name}: stopped; automatic startup disabled')
        elif args.action in {'start', 'restart'}:
            desired = service_plist(ROOT, name, settings.get('ocr_threads'))
            changed = not path.exists() or plistlib.loads(path.read_bytes()) != desired
            if loaded and (args.action == 'restart' or changed):
                launchctl('bootout', target)
                loaded = False
            if not loaded:
                path.write_bytes(plistlib.dumps(desired))
                path.chmod(0o600)
                launchctl('enable', target)
                bootstrap_agent(domain, path)
            print(f'{name}: {"already loaded" if loaded else "started"}; log: {ROOT / ".local-services" / (name + ".log")}')
        else:
            result = launchctl('print', target, check=False)
            if result.returncode:
                print(f'{name}: not loaded')
            else:
                fields, seen = [], set()
                for line in result.stdout.splitlines():
                    field = line.strip()
                    key = field.split(' = ', 1)[0]
                    if key in {'state', 'pid', 'last exit code'} and key not in seen:
                        fields.append(field)
                        seen.add(key)
                print(f'{name}: ' + '; '.join(fields))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        print(exc.stderr.strip())
        raise SystemExit(exc.returncode) from None
