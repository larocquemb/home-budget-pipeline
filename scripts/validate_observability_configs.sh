#!/usr/bin/env bash
set -euo pipefail

validation_repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$validation_repo_root"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker CLI is required. Start Colima and retry." >&2
  exit 2
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker is unavailable. Run 'colima start' and retry." >&2
  exit 2
fi

if [[ -x "$validation_repo_root/.venv/bin/python" ]]; then
  validation_python="$validation_repo_root/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  validation_python="$(command -v python3)"
else
  echo "Python 3 with PyYAML is required." >&2
  exit 2
fi

# Keep bind-mounted fixtures under the repository. Colima shares /Users on
# macOS but does not expose the host's /var/folders temporary directory.
validation_root="$(mktemp -d "$validation_repo_root/.observability-validation.XXXXXX")"
cleanup() {
  if [[ -d "$validation_root" ]]; then
    rm -r -- "$validation_root"
  fi
}
trap cleanup EXIT

printf 'Validating Prometheus configuration and rules...\n'
mkdir -p "$validation_root/prometheus/rules"
cp ops/monitoring/roles/monitoring/files/prometheus-home-budget-rules.yml \
  "$validation_root/prometheus/rules/home-budget.yml"
"$validation_python" - \
  "$validation_root/prometheus/rules/grafana-dashboard.yml" <<'PY'
import json
import pathlib
import re
import sys

import yaml

output = pathlib.Path(sys.argv[1])
dashboard_paths = (
    pathlib.Path(
        "ops/monitoring/roles/monitoring/templates/"
        "grafana-receipt-telemetry-dashboard.json.j2"
    ),
    pathlib.Path(
        "ops/monitoring/roles/monitoring/templates/"
        "grafana-ocr-performance-dashboard.json.j2"
    ),
)
grafana_values = {
    "${environment:regex}": "production",
    "${service:regex}": "home-budget-receipt-worker",
    "${worker_host:regex}": "k3s1",
    "${status:regex}": "succeeded",
    "${queue:regex}": r"receipts\.v1\.work",
    "${engine:regex}": "tesseract",
    "${dpi:regex}": "300",
    "${psm:regex}": "6",
    "${variant:regex}": "grayscale",
    "$__rate_interval": "5m",
    "$__range": "1h",
}

expressions = []
for dashboard_path in dashboard_paths:
    rendered = re.sub(r"\{\{[^{}]+\}\}", "validation", dashboard_path.read_text())
    dashboard = json.loads(rendered)
    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            expression = target.get("expr")
            if not expression:
                continue
            for variable, value in grafana_values.items():
                expression = expression.replace(variable, value)
            expressions.append(expression)

rules = {
    "groups": [
        {
            "name": "grafana-dashboard-validation",
            "rules": [
                {"record": f"grafana_dashboard_validation_{index:03d}", "expr": expr}
                for index, expr in enumerate(expressions, start=1)
            ],
        }
    ]
}
output.write_text(yaml.safe_dump(rules, sort_keys=False))
PY
cat > "$validation_root/prometheus/prometheus.yml" <<'EOF'
global:
  scrape_interval: 15s
  evaluation_interval: 30s

rule_files:
  - /etc/prometheus/rules/*.yml

scrape_configs:
  - job_name: alloy
    static_configs:
      - targets:
          - 127.0.0.1:12345

  - job_name: prometheus
    static_configs:
      - targets:
          - 127.0.0.1:9090
EOF
docker run --rm \
  --entrypoint /bin/promtool \
  --mount "type=bind,source=$validation_root/prometheus,target=/etc/prometheus,readonly" \
  prom/prometheus:v2.53.3 \
  check config /etc/prometheus/prometheus.yml

printf 'Validating OpenTelemetry Collector configuration...\n'
mkdir -p \
  "$validation_root/otel/server" \
  "$validation_root/otel/backend" \
  "$validation_root/otel/storage"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
  -subj '/CN=otel-validation' \
  -keyout "$validation_root/otel/server/tls.key" \
  -out "$validation_root/otel/server/tls.crt" \
  >/dev/null 2>&1
cp "$validation_root/otel/server/tls.crt" \
  "$validation_root/otel/server/ca.crt"
cp "$validation_root/otel/server/tls.key" \
  "$validation_root/otel/backend/tls.key"
cp "$validation_root/otel/server/tls.crt" \
  "$validation_root/otel/backend/tls.crt"
cp "$validation_root/otel/server/tls.crt" \
  "$validation_root/otel/backend/ca.crt"
docker run --rm \
  --env OTEL_TAIL_SAMPLING_DECISION_WAIT=10s \
  --env OTEL_TAIL_SAMPLING_PERCENTAGE=100 \
  --env OTEL_BACKEND_ENDPOINT=monitoring.invalid:4317 \
  --env OTEL_BACKEND_SERVER_NAME=monitoring.invalid \
  --mount "type=bind,source=$validation_repo_root/deploy/opentelemetry/collector-config.yaml,target=/etc/otelcol-contrib/config.yaml,readonly" \
  --mount "type=bind,source=$validation_root/otel/server,target=/var/run/otel/server,readonly" \
  --mount "type=bind,source=$validation_root/otel/backend,target=/var/run/otel/backend,readonly" \
  --mount "type=bind,source=$validation_root/otel/storage,target=/var/lib/otelcol/storage" \
  otel/opentelemetry-collector-contrib:0.160.0 \
  validate --config=/etc/otelcol-contrib/config.yaml

printf 'Validating external and local Alloy configurations...\n'
mkdir -p "$validation_root/alloy/tls"
cp deploy/external-logging/config.alloy "$validation_root/alloy/config.alloy"
cp deploy/local-logging/config.local.alloy \
  "$validation_root/alloy/config.local.alloy"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
  -subj '/CN=alloy-validation' \
  -keyout "$validation_root/alloy/loki-client.key" \
  -out "$validation_root/alloy/loki-client.crt" \
  >/dev/null 2>&1
cp "$validation_root/alloy/loki-client.crt" \
  "$validation_root/alloy/brown-rook-root-ca.crt"
cp "$validation_root/alloy/loki-client.crt" "$validation_root/alloy/tls/ca.crt"
cp "$validation_root/alloy/loki-client.key" \
  "$validation_root/alloy/otel-server.key"
cp "$validation_root/alloy/loki-client.crt" \
  "$validation_root/alloy/otel-server.crt"
cat > "$validation_root/alloy/home-budget.kubeconfig" <<'EOF'
apiVersion: v1
kind: Config
clusters:
  - name: validation
    cluster:
      insecure-skip-tls-verify: true
      server: https://kubernetes.invalid
users:
  - name: validation
    user:
      token: validation-only
contexts:
  - name: validation
    context:
      cluster: validation
      namespace: home-budget
      user: validation
current-context: validation
EOF
docker run --rm \
  --mount "type=bind,source=$validation_root/alloy,target=/etc/alloy,readonly" \
  grafana/alloy:v1.19.2 \
  validate /etc/alloy/config.alloy
docker run --rm \
  --mount "type=bind,source=$validation_root/alloy,target=/etc/alloy,readonly" \
  grafana/alloy:v1.19.0 \
  validate /etc/alloy/config.local.alloy

printf 'Validating Fluent Bit configuration...\n'
mkdir -p \
  "$validation_root/fluent-bit/tls" \
  "$validation_root/fluent-bit/buffers"
"$validation_python" - "$validation_root/fluent-bit" <<'PY'
import pathlib
import sys

import yaml

output = pathlib.Path(sys.argv[1])
resources = yaml.safe_load_all(
    pathlib.Path("deploy/fluent-bit/fluent-bit.yaml").read_text()
)
config = next(
    item
    for item in resources
    if item["kind"] == "ConfigMap"
    and item["metadata"]["name"] == "fluent-bit-config"
)["data"]
(output / "fluent-bit.conf").write_text(config["fluent-bit.conf"])
(output / "filters.lua").write_text(config["filters.lua"])
PY
openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
  -subj '/CN=fluent-bit-validation' \
  -keyout "$validation_root/fluent-bit/tls/tls.key" \
  -out "$validation_root/fluent-bit/tls/tls.crt" \
  >/dev/null 2>&1
cp "$validation_root/fluent-bit/tls/tls.crt" \
  "$validation_root/fluent-bit/tls/ca.crt"
fluent_bit_validation_log="$validation_root/fluent-bit-validation.log"
if ! docker run --rm \
  --env LOKI_HOST=monitoring.invalid \
  --env LOKI_PORT=3100 \
  --env LOKI_TLS_VHOST=monitoring.invalid \
  --env LOKI_CLUSTER=validation \
  --env LOKI_ENVIRONMENT=ci \
  --env FLUENT_BIT_LOG_LEVEL=info \
  --mount "type=bind,source=$validation_root/fluent-bit/fluent-bit.conf,target=/fluent-bit/etc/fluent-bit.conf,readonly" \
  --mount "type=bind,source=$validation_root/fluent-bit/filters.lua,target=/fluent-bit/etc/filters.lua,readonly" \
  --mount "type=bind,source=$validation_root/fluent-bit/tls,target=/fluent-bit/tls,readonly" \
  --mount "type=bind,source=$validation_root/fluent-bit/buffers,target=/buffers" \
  cr.fluentbit.io/fluent/fluent-bit:4.2.0 \
  --dry-run -c /fluent-bit/etc/fluent-bit.conf \
  2>&1 | tee "$fluent_bit_validation_log"; then
  echo "Fluent Bit configuration validation failed." >&2
  exit 1
fi
if grep -Fq '[error]' "$fluent_bit_validation_log"; then
  echo "Fluent Bit emitted an error during configuration validation." >&2
  exit 1
fi

printf 'All observability configurations are valid.\n'
