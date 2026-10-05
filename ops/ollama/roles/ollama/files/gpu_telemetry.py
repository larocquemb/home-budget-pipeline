"""Private read-only nvidia-smi endpoint; installed by ops/ollama Ansible."""
import argparse
import csv
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import socket
import subprocess


def read_gpus():
    output = subprocess.run([
        "nvidia-smi", "--query-gpu=index,uuid,utilization.gpu,memory.used,power.draw,temperature.gpu",
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
             "gpu_power_watts": number(row[4]), "gpu_temperature_c": number(row[5])}
            for row in csv.reader(output.splitlines())]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
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
    args = parser.parse_args()
    HTTPServer((args.host, args.port), Handler).serve_forever()
