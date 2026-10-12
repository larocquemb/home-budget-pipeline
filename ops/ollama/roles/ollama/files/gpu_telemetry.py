"""Private read-only nvidia-smi endpoint; installed by ops/ollama Ansible."""
import argparse
import csv
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
import os
from pathlib import Path
import socket
import subprocess
from urllib.request import ProxyHandler, build_opener


def read_gpus():
    output = subprocess.run([
        "nvidia-smi", "--query-gpu=index,uuid,utilization.gpu,memory.used,power.draw,temperature.gpu,memory.total",
        "--format=csv,noheader,nounits",
    ], check=True, capture_output=True, text=True, timeout=2).stdout
    def number(text):
        try:
            return float(text.strip())
        except ValueError:
            return None
    return [{"gpu_index": int(row[0]), "gpu_uuid": row[1].strip(),
             "gpu_util_percent": number(row[2]),
             "gpu_memory_bytes": int(number(row[3]) * 1024 ** 2) if number(row[3]) is not None else None,
             "gpu_power_watts": number(row[4]), "gpu_temperature_c": number(row[5]),
             "gpu_memory_total_bytes": int(number(row[6]) * 1024 ** 2)
             if len(row) > 6 and number(row[6]) is not None else None}
            for row in csv.reader(output.splitlines())]


def read_host(proc=Path('/proc')):
    memory = {line.split(':')[0]: int(line.split()[1]) * 1024
              for line in (proc / 'meminfo').read_text().splitlines()
              if line.split(':')[0] in {'MemTotal', 'MemAvailable', 'SwapTotal', 'SwapFree'}}
    cpu = [int(value) for value in (proc / 'stat').read_text().splitlines()[0].split()[1:9]]
    # guest time is already included in user/nice; do not count it twice.
    return memory, cpu


def read_service(cgroup=Path('/sys/fs/cgroup/system.slice/ollama.service')):
    cpu = dict(line.split() for line in (cgroup / 'cpu.stat').read_text().splitlines())
    return {'cpu_seconds_total': int(cpu['usage_usec']) / 1e6,
            'memory_bytes': int((cgroup / 'memory.current').read_text()),
            'swap_bytes': int((cgroup / 'memory.swap.current').read_text())}


def read_models(base):
    opener = build_opener(ProxyHandler({}))
    with opener.open(base.rstrip('/') + '/api/ps', timeout=2) as response:
        models = json.load(response)['models']
    if not isinstance(models, list):
        raise ValueError('Invalid model inventory')
    return models[:16]


def prometheus_metrics(cgroup, ollama_url):
    lines = []
    declared = set()
    def metric(name, value, description, kind='gauge', labels=None):
        if value is None or not math.isfinite(float(value)):
            return
        if name not in declared:
            lines.extend([f'# HELP {name} {description}', f'# TYPE {name} {kind}'])
            declared.add(name)
        suffix = ('{' + ','.join(f'{key}={json.dumps(str(val))}'
                                for key, val in labels.items()) + '}') if labels else ''
        lines.append(f'{name}{suffix} {value}')

    try:
        memory, cpu = read_host()
        for key, label in [('MemTotal', 'total'), ('MemAvailable', 'available'),
                           ('SwapTotal', 'swap_total'), ('SwapFree', 'swap_free')]:
            metric('ollama_host_memory_' + label + '_bytes', memory[key], 'Host memory bytes')
        ticks = os.sysconf('SC_CLK_TCK')
        for mode, value in zip(('user', 'nice', 'system', 'idle', 'iowait', 'irq', 'softirq', 'steal'), cpu):
            metric('ollama_host_cpu_seconds_total', value / ticks, 'Aggregate host CPU time',
                   'counter', {'mode': mode})
        metric('ollama_host_collector_success', 1, 'Host resource collection succeeded')
    except (OSError, ValueError, KeyError, IndexError):
        metric('ollama_host_collector_success', 0, 'Host resource collection succeeded')
    try:
        service = read_service(cgroup)
        for key, value in service.items():
            metric('ollama_service_' + key, value, 'Managed Ollama service including all child runners',
                   'counter' if key.endswith('_total') else 'gauge')
        metric('ollama_service_collector_success', 1, 'Service cgroup collection succeeded')
    except (OSError, ValueError, KeyError):
        metric('ollama_service_collector_success', 0, 'Service cgroup collection succeeded')
    try:
        gpus = read_gpus()
        for gpu in gpus:
            for key in ('gpu_util_percent', 'gpu_memory_bytes', 'gpu_memory_total_bytes',
                        'gpu_power_watts', 'gpu_temperature_c'):
                metric('ollama_' + key, gpu[key], 'Host GPU statistic; includes other GPU workloads',
                       labels={'gpu_index': gpu['gpu_index']})
        metric('ollama_gpu_collector_success', 1, 'NVIDIA GPU collection succeeded')
    except (OSError, subprocess.SubprocessError, ValueError):
        metric('ollama_gpu_collector_success', 0, 'NVIDIA GPU collection succeeded')
    try:
        models = read_models(ollama_url)
        metric('ollama_loaded_models', len(models), 'Models loaded in the managed Ollama server')
        for model in models:
            labels = {'model': model.get('name', 'unknown')}
            for field in ('size', 'size_vram', 'context_length'):
                metric('ollama_model_' + field, model.get(field), 'Loaded model allocation from Ollama api/ps', labels=labels)
        metric('ollama_api_up', 1, 'Managed Ollama inventory API is reachable')
    except (OSError, ValueError, KeyError, TypeError):
        metric('ollama_api_up', 0, 'Managed Ollama inventory API is reachable')
    return '\n'.join(lines) + '\n'


class Handler(BaseHTTPRequestHandler):
    cgroup = Path('/sys/fs/cgroup/system.slice/ollama.service')
    ollama_url = 'http://192.168.2.201:11434'

    def do_GET(self):
        if self.path == '/metrics':
            body = prometheus_metrics(self.cgroup, self.ollama_url).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; version=0.0.4; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path != "/gpu":
            self.send_error(404)
            return
        try:
            payload = {"timestamp": datetime.now(timezone.utc).isoformat(),
                       "gpu_host": socket.gethostname(), "gpus": read_gpus()}
            status = 200
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            payload, status = {"error_type": type(exc).__name__}, 503
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=11435)
    parser.add_argument('--cgroup', type=Path, default=Handler.cgroup)
    parser.add_argument('--ollama-url', default=Handler.ollama_url)
    args = parser.parse_args()
    Handler.cgroup, Handler.ollama_url = args.cgroup, args.ollama_url
    HTTPServer((args.host, args.port), Handler).serve_forever()
