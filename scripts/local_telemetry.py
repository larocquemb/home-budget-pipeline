#!/usr/bin/env python3
"""Configure local Alloy to send directly to the LAN monitoring backends."""
from __future__ import annotations
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys

import receipt_services
from postgres_credentials import read_env

ROOT = Path(__file__).resolve().parents[1]


def update_settings(path: Path, updates: dict[str, str]) -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    retained = [line for line in lines if line.split('=', 1)[0].strip().removeprefix('export ') not in updates]
    path.write_text('\n'.join(retained + [f'{key}={shlex.quote(value)}' for key, value in updates.items()]) + '\n')
    path.chmod(0o600)


def prepare() -> None:
    alloy = shutil.which('alloy')
    if not alloy:
        raise RuntimeError('Install Alloy first: brew install grafana-alloy')
    monitoring = read_env(ROOT / '.env.monitoring')
    pki = Path(monitoring['MONITORING_PKI_DIR']).expanduser()
    state = ROOT / '.local-services/telemetry'
    tls = state / 'tls'
    tls.mkdir(parents=True, mode=0o700, exist_ok=True)
    files = {
        'LOCAL_TELEMETRY_CA': pki / 'root/root_ca.crt',
        'LOCAL_LOKI_CERT': pki / 'leafs/alloy-loki-client/alloy-loki-client.fullchain.crt',
        'LOCAL_LOKI_KEY': pki / 'leafs/alloy-loki-client/alloy-loki-client.key',
        'LOCAL_OTLP_CERT': pki / 'leafs/otel-collector-backend-client/otel-collector-backend-client.fullchain.crt',
        'LOCAL_OTLP_KEY': pki / 'leafs/otel-collector-backend-client/otel-collector-backend-client.key',
    }
    environment = {}
    for key, source in files.items():
        target = tls / source.name
        shutil.copyfile(source, target)
        target.chmod(0o600)
        environment[key] = str(target)
    for key, name in [('LOCAL_WORKER_LOG', 'worker'), ('LOCAL_COLLECTOR_LOG', 'collector'),
                      ('LOCAL_PROBE_LOG', 'telemetry-probe'),
                      ('LOCAL_ENRICHMENT_LOG', 'enrichment'), ('LOCAL_PUBLISHER_LOG', 'publisher'),
                      ('LOCAL_CLI_LOG', 'cli'), ('LOCAL_WEB_LOG', 'web'), ('LOCAL_ALLOY_LOG', 'alloy')]:
        log = ROOT / '.local-services' / f'{name}.log'
        log.touch(mode=0o600, exist_ok=True)
        environment[key] = str(log)
    environment.update({
        'LOCAL_TELEMETRY_HOST': socket.gethostname().split('.')[0],
        'LOCAL_ALLOY_BINARY': alloy,
    })
    update_settings(state / 'agent.env', environment)
    validation_environment = os.environ.copy()
    validation_environment.update(environment)
    subprocess.run([alloy, 'validate', str(ROOT / 'deploy/local-telemetry/config.alloy')],
                   env=validation_environment, check=True)
    update_settings(ROOT / '.env.dev', {
        'HOME_BUDGET_TELEMETRY_ENABLED': 'true',
        'HOME_BUDGET_STRUCTURED_LOGS': 'true',
        'HOME_BUDGET_LOCAL_LOG_DIR': str(ROOT / '.local-services'),
        'OTEL_EXPORTER_OTLP_ENDPOINT': 'http://127.0.0.1:4318',
        'OTEL_EXPORTER_OTLP_PROTOCOL': 'http/protobuf',
        'OTEL_EXPORTER_OTLP_INSECURE': 'true',
        'OTEL_DEPLOYMENT_ENVIRONMENT': 'development',
        'OTEL_METRIC_EXPORT_INTERVAL': '5000',
    })


if __name__ == '__main__':
    action = sys.argv[1] if len(sys.argv) > 1 else ''
    try:
        if action in {'start', 'restart'}:
            prepare()
        elif action == 'stop':
            update_settings(ROOT / '.env.dev', {'HOME_BUDGET_TELEMETRY_ENABLED': 'false'})
        receipt_services.NAMES = ('alloy',)
        receipt_services.main()
        if action in {'start', 'restart', 'stop'}:
            sys.argv[1] = 'restart'
            receipt_services.NAMES = None
            receipt_services.main()
    except (RuntimeError, OSError, KeyError, subprocess.CalledProcessError) as exc:
        print(f'Local telemetry setup failed: {exc}', file=sys.stderr)
        raise SystemExit(2) from None
