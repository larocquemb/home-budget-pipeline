import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


def _render(path: str) -> dict[tuple[str, str], dict]:
    if not shutil.which("kubectl"):
        pytest.skip("kubectl is not installed")
    rendered = subprocess.check_output(["kubectl", "kustomize", str(ROOT / path)], text=True)
    return {(obj["kind"], obj["metadata"]["name"]): obj for obj in yaml.safe_load_all(rendered)}


def test_external_reader_is_namespace_scoped_and_has_no_secret_access():
    resources = _render("k8s")
    account = resources[("ServiceAccount", "external-log-reader")]
    role = resources[("Role", "external-log-reader")]
    binding = resources[("RoleBinding", "external-log-reader")]

    assert account["metadata"]["namespace"] == "home-budget"
    assert account["automountServiceAccountToken"] is False
    assert role["metadata"]["namespace"] == "home-budget"
    assert role["rules"] == [
        {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "watch"]},
        {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
    ]
    assert binding["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "Role",
        "name": "external-log-reader",
    }


def test_external_token_template_is_not_deployed_or_populated():
    template = yaml.safe_load((ROOT / "k8s/external-log-reader-token.example.yaml").read_text())
    kustomization = (ROOT / "k8s/kustomization.yaml").read_text()

    assert template["type"] == "kubernetes.io/service-account-token"
    assert template["metadata"]["annotations"]["kubernetes.io/service-account.name"] == "external-log-reader"
    assert "data" not in template
    assert "stringData" not in template
    assert "external-log-reader-token.example.yaml" not in kustomization


def test_external_alloy_uses_kubernetes_api_and_verified_loki_tls():
    config = (ROOT / "deploy/external-logging/config.alloy").read_text()

    assert config.count('kubeconfig_file = "/etc/alloy/home-budget.kubeconfig"') == 2
    assert 'names = ["home-budget"]' in config
    assert 'cluster     = "brownrook-k3s1"' in config
    assert 'url = "https://127.0.0.1:3100/loki/api/v1/push"' in config
    assert 'ca_file     = "/etc/alloy/brown-rook-root-ca.crt"' in config
    assert 'cert_file   = "/etc/alloy/loki-client.crt"' in config
    assert 'key_file    = "/etc/alloy/loki-client.key"' in config
    assert 'server_name = "monitoring.idc.brownrook.net"' in config
    assert 'min_version = "TLS12"' in config
    assert "insecure_skip_verify" not in config
    assert "wal {\n    enabled = true" in config


def test_loki_listener_requires_private_ca_client_certificates():
    config = yaml.safe_load(
        (ROOT / "deploy/external-logging/loki-tls-config.example.yaml").read_text()
    )["server"]

    assert config["http_listen_address"] == "0.0.0.0"
    assert config["tls_min_version"] == "VersionTLS12"
    assert config["http_tls_config"]["client_auth_type"] == (
        "RequireAndVerifyClientCert"
    )
    assert config["http_tls_config"]["client_ca_file"].endswith(
        "brown-rook-client-ca.crt"
    )


def test_grafana_datasource_template_requires_verified_client_tls():
    datasource = yaml.safe_load(
        (
            ROOT
            / "deploy/external-logging/grafana-datasource.example.yaml"
        ).read_text()
    )["datasources"][0]

    assert datasource["url"] == "https://monitoring.idc.brownrook.net:3100"
    assert datasource["jsonData"] == {
        "tlsAuth": True,
        "tlsAuthWithCACert": True,
        "tlsSkipVerify": False,
        "serverName": "monitoring.idc.brownrook.net",
    }
    assert set(datasource["secureJsonData"]) == {
        "tlsCACert",
        "tlsClientCert",
        "tlsClientKey",
    }


def test_exported_kubeconfigs_are_ignored():
    assert "*.kubeconfig" in (ROOT / ".gitignore").read_text().splitlines()


def test_monitoring_role_bounds_loki_and_journald_output():
    defaults = yaml.safe_load(
        (
            ROOT
            / "ops/monitoring/roles/monitoring/defaults/main.yml"
        ).read_text()
    )
    loki = (
        ROOT
        / "ops/monitoring/roles/monitoring/templates/loki-config.yml.j2"
    ).read_text()
    journal = (
        ROOT
        / "ops/monitoring/roles/monitoring/templates/journald-monitoring.conf.j2"
    ).read_text()
    tasks = (
        ROOT
        / "ops/monitoring/roles/monitoring/tasks/main.yml"
    ).read_text()
    handlers = (
        ROOT
        / "ops/monitoring/roles/monitoring/handlers/main.yml"
    ).read_text()
    assert defaults["monitoring_loki_log_level"] == "info"
    assert "log_level: {{ monitoring_loki_log_level }}" in loki
    assert "log_level: debug" not in loki
    assert defaults["monitoring_journal_system_max_use"] == "256M"
    assert defaults["monitoring_journal_runtime_max_use"] == "64M"
    assert defaults["monitoring_journal_rate_limit_burst"] == 5000
    assert "SystemMaxUse={{ monitoring_journal_system_max_use }}" in journal
    assert "SystemKeepFree={{ monitoring_journal_system_keep_free }}" in journal
    assert "MaxRetentionSec={{ monitoring_journal_max_retention }}" in journal
    assert "RateLimitBurst={{ monitoring_journal_rate_limit_burst }}" in journal
    assert "dest: /etc/systemd/journald.conf.d/monitoring-limits.conf" in tasks
    assert "notify: restart journald" in tasks
    assert "name: systemd-journald" in handlers
    assert "listen: restart journald" in handlers


def test_monitoring_role_uses_durable_loki_storage_with_retention():
    defaults = yaml.safe_load(
        (
            ROOT
            / "ops/monitoring/roles/monitoring/defaults/main.yml"
        ).read_text()
    )
    loki = (
        ROOT
        / "ops/monitoring/roles/monitoring/templates/loki-config.yml.j2"
    ).read_text()
    tasks = (
        ROOT
        / "ops/monitoring/roles/monitoring/tasks/main.yml"
    ).read_text()

    assert defaults["monitoring_loki_storage_path"] == "/var/lib/loki"
    assert defaults["monitoring_loki_retention_period"] == "336h"
    assert defaults["monitoring_loki_retention_delete_worker_count"] == 10
    assert "path_prefix: {{ monitoring_loki_storage_path }}" in loki
    assert (
        "working_directory: {{ monitoring_loki_storage_path }}/compactor" in loki
    )
    assert "retention_enabled: true" in loki
    assert "retention_period: {{ monitoring_loki_retention_period }}" in loki
    assert "delete_request_store: filesystem" in loki
    assert "/tmp/loki" not in loki
    assert "- name: Create durable Loki storage" in tasks
    assert 'path: "{{ monitoring_loki_storage_path }}"' in tasks
    assert "owner: loki\n    group: root" in tasks
    assert "- name: Remove retired temporary Loki storage" in tasks
    assert "path: /tmp/loki\n    state: absent" in tasks
    assert tasks.index("- name: Wait for authenticated Loki readiness") < tasks.index(
        "- name: Remove retired temporary Loki storage"
    )


def test_monitoring_role_reconciles_mtls_telemetry_backend():
    defaults = yaml.safe_load(
        (
            ROOT
            / "ops/monitoring/roles/monitoring/defaults/main.yml"
        ).read_text()
    )
    inventory = yaml.safe_load(
        (ROOT / "ops/monitoring/inventory/group_vars/monitoring.yml").read_text()
    )
    alloy = (ROOT / "deploy/external-logging/config.alloy").read_text()
    tempo = (
        ROOT
        / "ops/monitoring/roles/monitoring/templates/tempo-config.yml.j2"
    ).read_text()
    firewall = (
        ROOT
        / "ops/monitoring/roles/monitoring/templates/nftables.conf.j2"
    ).read_text()
    tasks = (
        ROOT
        / "ops/monitoring/roles/monitoring/tasks/main.yml"
    ).read_text()
    handlers = (
        ROOT
        / "ops/monitoring/roles/monitoring/handlers/main.yml"
    ).read_text()
    identity_validation = (
        ROOT
        / "ops/monitoring/roles/monitoring/tasks/validate_identity.yml"
    ).read_text()
    ci = (ROOT / ".github/workflows/ci.yml").read_text()

    assert defaults["monitoring_tempo_version"] == "3.0.3"
    assert defaults["monitoring_tempo_storage_path"] == "/var/lib/tempo"
    assert defaults["monitoring_tempo_retention_period"] == "336h"
    assert "--web.listen-address=127.0.0.1:9090" in defaults[
        "monitoring_prometheus_args"
    ]
    assert "--web.enable-remote-write-receiver" in defaults[
        "monitoring_prometheus_args"
    ]
    assert "--enable-feature=exemplar-storage" in defaults[
        "monitoring_prometheus_args"
    ]

    assert inventory["monitoring_otel_port"] == 4317
    assert inventory["monitoring_otel_allowed_ipv4_sources"] == [
        "192.168.2.230/32"
    ]
    assert inventory["monitoring_grafana_tempo_datasource_uid"]

    assert 'otelcol.receiver.otlp "home_budget"' in alloy
    assert 'endpoint          = "0.0.0.0:4317"' in alloy
    assert 'client_ca_file = "/etc/alloy/brown-rook-root-ca.crt"' in alloy
    assert 'otelcol.processor.memory_limiter "home_budget"' in alloy
    assert 'otelcol.processor.batch "home_budget"' in alloy
    assert 'otelcol.exporter.otlp "tempo"' in alloy
    assert "sending_queue {\n    enabled = false" in alloy
    assert 'endpoint = "127.0.0.1:14317"' in alloy
    assert 'url = "http://127.0.0.1:9090/api/v1/write"' in alloy
    assert "insecure_skip_verify" not in alloy

    assert "http_listen_address: {{ monitoring_tempo_http_address }}" in tempo
    assert "endpoint: {{ monitoring_tempo_otlp_address }}:" in tempo
    assert "backend: local" in tempo
    assert "block_retention: {{ monitoring_tempo_retention_period }}" in tempo
    assert "path: {{ monitoring_tempo_storage_path }}/wal" in tempo
    assert "path: {{ monitoring_tempo_storage_path }}/blocks" in tempo

    assert "tempo={{ monitoring_tempo_version }}" in tasks
    assert "policy_rc_d: 101" in tasks
    assert "validate: /usr/bin/tempo --config.file=%s --config.verify" in tasks
    assert "- name: Wait for Tempo readiness on loopback" in tasks
    assert "- name: Wait for Prometheus readiness on loopback" in tasks
    assert "- name: Wait for Alloy OTLP listener" in tasks
    assert "monitoring_receipt_telemetry_secret_name" in tasks
    assert "monitoring_otel_collector_server_secret_name" in tasks
    assert "monitoring_otel_collector_backend_secret_name" in tasks
    assert "monitoring_otel_backend_secret_name" in tasks
    assert "tracesToLogsV2:" in tasks
    assert "matcherRegex:" in tasks
    assert "-checkhost" in identity_validation
    assert "tcp dport {{ monitoring_otel_port }} drop" in firewall
    assert "listen: restart Tempo" in handlers
    assert "listen: restart Prometheus" in handlers
    assert '"$RUNNER_TEMP/alloy/otel-server.key"' in ci
    assert '"$RUNNER_TEMP/alloy/otel-server.crt"' in ci
