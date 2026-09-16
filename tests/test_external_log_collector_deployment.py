import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


def _v2_dashboard_spec(dashboard: dict) -> dict:
    assert dashboard["apiVersion"] == "dashboard.grafana.app/v2"
    assert dashboard["kind"] == "Dashboard"
    return dashboard["spec"]


def _v2_dashboard_panels(dashboard: dict) -> list[dict]:
    return [
        element["spec"]
        for element in _v2_dashboard_spec(dashboard)["elements"].values()
        if element["kind"] == "Panel"
    ]


def _v2_panel_query_models(panel: dict) -> list[dict]:
    return [query["spec"] for query in panel["data"]["spec"]["queries"]]


def _v2_panel_targets(panel: dict) -> list[dict]:
    return [
        query["query"]["spec"]
        for query in _v2_panel_query_models(panel)
    ]


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
    site = yaml.safe_load((ROOT / "ops/monitoring/site.yml").read_text())
    validator = (ROOT / "scripts/validate_observability_configs.sh").read_text()

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
    assert defaults["monitoring_alloy_metrics_target"] == "127.0.0.1:12345"

    assert inventory["monitoring_otel_port"] == 4317
    assert inventory["monitoring_otel_allowed_ipv4_sources"] == [
        "192.168.2.230/32"
    ]
    assert inventory["monitoring_grafana_tempo_datasource_uid"]

    assert 'otelcol.receiver.otlp "home_budget"' in alloy
    assert 'endpoint          = "0.0.0.0:4317"' in alloy
    assert 'client_ca_file = "/etc/alloy/brown-rook-root-ca.crt"' in alloy
    assert 'min_version = "1.2"' in alloy
    assert '"TLS 1.2"' not in alloy
    assert 'otelcol.processor.memory_limiter "home_budget"' in alloy
    assert 'otelcol.processor.batch "home_budget"' in alloy
    assert 'otelcol.processor.transform "home_budget_metric_labels"' in alloy
    assert 'attributes["environment"]' in alloy
    assert 'attributes["service"]' in alloy
    assert 'attributes["worker_host"]' in alloy
    assert 'prometheus.exporter.self "home_budget"' not in alloy
    assert 'prometheus.scrape "alloy_self"' not in alloy
    assert 'otelcol.exporter.otlp "tempo"' in alloy
    assert "sending_queue {\n    enabled = false" in alloy
    assert 'endpoint = "127.0.0.1:14317"' in alloy
    assert 'url = "http://127.0.0.1:9090/api/v1/write"' in alloy
    assert "url = \"http://127.0.0.1:9090/api/v1/write\"\n\n    queue_config {" in alloy
    assert "insecure_skip_verify" not in alloy

    assert "http_listen_address: {{ monitoring_tempo_http_address }}" in tempo
    assert "endpoint: {{ monitoring_tempo_otlp_address }}:" in tempo
    assert "backend: local" in tempo
    assert "backend_worker:" in tempo
    assert "block_retention: {{ monitoring_tempo_retention_period }}" in tempo
    assert "compactor:" not in tempo
    assert "path: {{ monitoring_tempo_storage_path }}/wal" in tempo
    assert "path: {{ monitoring_tempo_storage_path }}/blocks" in tempo

    assert "tempo={{ monitoring_tempo_version }}" in tasks
    assert "policy_rc_d: 101" in tasks
    assert "validate: /usr/bin/tempo --config.file=%s --config.verify=true" in tasks
    assert "- name: Probe Alloy OTLP listener before readiness checks" in tasks
    assert (
        "- name: Restart Alloy when its installed OTLP configuration is not active"
        in tasks
    )
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
    assert '"$validation_root/alloy/otel-server.key"' in validator
    assert '"$validation_root/alloy/otel-server.crt"' in validator
    assert "grafana/alloy:v1.19.2" in validator
    assert site[0]["force_handlers"] is True


def test_monitoring_role_completes_receipt_metrics_observability():
    defaults = yaml.safe_load(
        (ROOT / "ops/monitoring/roles/monitoring/defaults/main.yml").read_text()
    )
    inventory = yaml.safe_load(
        (ROOT / "ops/monitoring/inventory/group_vars/monitoring.yml").read_text()
    )
    tasks = (ROOT / "ops/monitoring/roles/monitoring/tasks/main.yml").read_text()
    handlers = (ROOT / "ops/monitoring/roles/monitoring/handlers/main.yml").read_text()
    validator = (ROOT / "scripts/validate_observability_configs.sh").read_text()
    rules = yaml.safe_load(
        (
            ROOT
            / "ops/monitoring/roles/monitoring/files/prometheus-home-budget-rules.yml"
        ).read_text()
    )

    assert defaults["monitoring_prometheus_config_path"] == (
        "/etc/prometheus/prometheus.yml"
    )
    assert defaults["monitoring_prometheus_rules_path"] == (
        "/etc/prometheus/rules/home-budget.yml"
    )
    assert inventory["monitoring_grafana_prometheus_datasource_uid"] == (
        "home-budget-prometheus"
    )
    assert inventory["monitoring_grafana_receipt_dashboard_uid"] == (
        "home-budget-receipt-telemetry"
    )
    assert inventory["monitoring_grafana_ocr_dashboard_uid"] == (
        "home-budget-ocr-performance"
    )
    assert inventory["monitoring_grafana_receipt_dashboard_revision"] == "kan-88-v2"
    assert defaults["monitoring_grafana_api_namespace"] == "default"
    assert defaults["monitoring_grafana_home_budget_folder_uid"] == "home-budget"
    assert defaults["monitoring_grafana_home_budget_folder_title"] == "Home Budget"
    assert defaults["monitoring_grafana_brown_rook_folder_uid"] == "brown-rook"
    assert defaults["monitoring_grafana_brown_rook_folder_title"] == "Brown Rook"
    assert not (
        ROOT
        / "ops/monitoring/roles/monitoring/templates/grafana-dashboard-provider.yml.j2"
    ).exists()

    groups = {group["name"]: group for group in rules["groups"]}
    assert set(groups) == {"home-budget-recording", "home-budget-alerts"}
    recordings = {
        rule["record"] for rule in groups["home-budget-recording"]["rules"]
    }
    assert recordings == {
        "job_status:brownrook_receipt_processed:rate5m",
        "job:brownrook_receipt_processing_duration_seconds:p95_5m",
        "job_engine_status:brownrook_receipt_ocr_pass_completed:rate5m",
        "job_engine:brownrook_receipt_ocr_pass_duration_seconds:p95_5m",
        "job_event_outcome:brownrook_ocr_collector_events:rate5m",
        "job_event:brownrook_ocr_collector_persistence_duration_seconds:p95_5m",
        "job:brownrook_receipt_cache_hit:ratio5m",
        "queue:rabbitmq_receipt_messages_ready",
        "queue:rabbitmq_receipt_oldest_message_age_seconds",
    }
    alerts = {rule["alert"] for rule in groups["home-budget-alerts"]["rules"]}
    assert alerts == {
        "HomeBudgetTelemetryMetricsMissing",
        "HomeBudgetReceiptProcessingFailed",
        "HomeBudgetOcrPassFailureRatioHigh",
        "HomeBudgetReceiptProcessingLatencyHigh",
        "HomeBudgetReviewRequiredRatioHigh",
        "HomeBudgetReceiptQueueGrowing",
        "HomeBudgetReceiptJobStale",
        "HomeBudgetReceiptDeadLettered",
        "HomeBudgetOcrResultDeadLettered",
        "HomeBudgetTelemetryDeliveryFailure",
    }
    rules_text = (
        ROOT
        / "ops/monitoring/roles/monitoring/files/prometheus-home-budget-rules.yml"
    ).read_text()
    assert "brownrook_receipt_processed_total" in rules_text
    assert "brownrook_receipt_processing_duration_seconds_bucket" in rules_text
    assert "run_uuid" not in rules_text
    assert "pass_id" not in rules_text
    assert "source_reference" not in rules_text
    assert 'absent_over_time(up{job="alloy"}[5m])' in rules_text

    datasource_text = (
        ROOT
        / "ops/monitoring/roles/monitoring/templates/grafana-prometheus-datasource.yml.j2"
    ).read_text()
    datasource_text = datasource_text.replace(
        "{{ monitoring_grafana_prometheus_datasource_name }}",
        "home-budget-prometheus",
    ).replace(
        "{{ monitoring_grafana_prometheus_datasource_uid }}",
        "home-budget-prometheus",
    ).replace(
        "{{ monitoring_grafana_tempo_datasource_uid }}",
        "home-budget-tempo",
    ).replace(
        "{{ monitoring_prometheus_version }}",
        "2.53.3",
    )
    datasource = yaml.safe_load(datasource_text)["datasources"][0]
    assert datasource["url"] == "http://127.0.0.1:9090"
    assert datasource["jsonData"]["exemplarTraceIdDestinations"] == [
        {"name": "trace_id", "datasourceUid": "home-budget-tempo"}
    ]

    dashboard_text = (
        ROOT
        / "ops/monitoring/roles/monitoring/templates/grafana-receipt-telemetry-dashboard.json.j2"
    ).read_text()
    dashboard_text = dashboard_text.replace(
        "{{ monitoring_grafana_prometheus_datasource_uid }}",
        "home-budget-prometheus",
    ).replace(
        "{{ monitoring_grafana_home_budget_folder_uid_effective }}",
        "home-budget",
    ).replace(
        "{{ monitoring_grafana_receipt_dashboard_uid }}",
        "home-budget-receipt-telemetry",
    ).replace(
        "{{ monitoring_grafana_ocr_dashboard_uid }}",
        "home-budget-ocr-performance",
    ).replace(
        "{{ monitoring_grafana_receipt_dashboard_revision }}",
        "kan-88-v2",
    )
    dashboard = json.loads(dashboard_text)
    dashboard_spec = _v2_dashboard_spec(dashboard)
    dashboard_panels = _v2_dashboard_panels(dashboard)
    assert dashboard["metadata"]["name"] == "home-budget-receipt-telemetry"
    assert dashboard["metadata"]["annotations"]["grafana.app/folder"] == (
        "home-budget"
    )
    assert dashboard_spec["title"] == "Home Budget Receipt Telemetry"
    assert len(dashboard_panels) == 11
    assert {variable["spec"]["name"] for variable in dashboard_spec["variables"]} == {
        "environment", "service", "worker_host", "status", "queue",
    }
    assert all(
        query["query"]["datasource"]["name"] == "home-budget-prometheus"
        for panel in dashboard_panels
        for query in _v2_panel_query_models(panel)
    )
    assert any(
        target.get("exemplar") is True
        for panel in dashboard_panels
        for target in _v2_panel_targets(panel)
    )
    overview_expressions = "\n".join(
        target["expr"]
        for panel in dashboard_panels
        for target in _v2_panel_targets(panel)
    )
    assert "rabbitmq_detailed_queue_messages_ready" in overview_expressions
    assert "otelcol_exporter_send_failed_metric_points_total" in overview_expressions
    assert "fluentbit_output_dropped_records_total" in overview_expressions
    assert "prometheus_remote_storage_samples_failed_total" in overview_expressions
    assert "prometheus_remote_storage_samples_dropped_total" in overview_expressions
    assert "prometheus_remote_storage_samples_retries_total" in overview_expressions
    assert "prometheus_remote_storage_enqueue_retries_total" in overview_expressions
    assert "brownrook_ocr_collector_events_total" in overview_expressions
    assert "brownrook_ocr_collector_persistence_duration_seconds_bucket" in overview_expressions
    assert "brownrook_ocr_collector_routed_total" in overview_expressions
    assert "ocr.results.v1.dead" in overview_expressions
    assert 'component_id=\\"prometheus.remote_write.local_prometheus\\"' in dashboard_text
    assert "grafana-dashboard-validation" in validator
    assert "grafana-receipt-telemetry-dashboard.json.j2" in validator
    assert "grafana-ocr-performance-dashboard.json.j2" in validator
    assert "grafana-public-live-demo-dashboard.json.j2" in validator
    assert '"${queue:regex}": r"receipts\\.v1\\.work"' in validator
    assert '"$__rate_interval": "5m"' in validator
    assert '"$__range": "1h"' in validator
    rabbitmq_expressions = [
        target["expr"]
        for panel in dashboard_panels
        for target in _v2_panel_targets(panel)
        if "rabbitmq_" in target["expr"]
    ]
    assert len(rabbitmq_expressions) == 8
    variable_queue_expressions = [expr for expr in rabbitmq_expressions if "ocr.results.v1.dead" not in expr]
    assert all('queue=~`${queue:regex}`' in expr for expr in variable_queue_expressions)
    assert all('queue=~"${queue:regex}"' not in expr for expr in variable_queue_expressions)
    idle_zero_panels = {
        "Receipts processed in selected period",
        "Receipt failure ratio",
        "Review-required ratio",
        "OCR cache hit ratio",
    }
    assert all(
        _v2_panel_targets(panel)[0]["expr"].endswith("or vector(0)")
        for panel in dashboard_panels
        if panel["title"] in idle_zero_panels
    )
    panels_by_title = {panel["title"]: panel for panel in dashboard_panels}
    receipt_count = panels_by_title["Receipts processed in selected period"]
    assert receipt_count["vizConfig"]["spec"]["fieldConfig"]["defaults"]["unit"] == "short"
    assert "increase(brownrook_receipt_processed_total" in _v2_panel_targets(
        receipt_count
    )[0]["expr"]
    assert "[$__range]" in _v2_panel_targets(receipt_count)[0]["expr"]
    receipt_throughput = panels_by_title["Receipt throughput (receipts/minute)"]
    assert receipt_throughput["vizConfig"]["spec"]["fieldConfig"]["defaults"]["unit"] == "short"
    assert _v2_panel_targets(receipt_throughput)[0]["expr"].endswith("* 60")

    ocr_dashboard_text = (
        ROOT
        / "ops/monitoring/roles/monitoring/templates/grafana-ocr-performance-dashboard.json.j2"
    ).read_text()
    ocr_dashboard_text = ocr_dashboard_text.replace(
        "{{ monitoring_grafana_prometheus_datasource_uid }}",
        "home-budget-prometheus",
    ).replace(
        "{{ monitoring_grafana_home_budget_folder_uid_effective }}",
        "home-budget",
    ).replace(
        "{{ monitoring_grafana_receipt_dashboard_uid }}",
        "home-budget-receipt-telemetry",
    ).replace(
        "{{ monitoring_grafana_ocr_dashboard_uid }}",
        "home-budget-ocr-performance",
    ).replace(
        "{{ monitoring_grafana_receipt_dashboard_revision }}",
        "kan-88-v2",
    )
    ocr_dashboard = json.loads(ocr_dashboard_text)
    ocr_dashboard_spec = _v2_dashboard_spec(ocr_dashboard)
    ocr_dashboard_panels = _v2_dashboard_panels(ocr_dashboard)
    assert ocr_dashboard["metadata"]["name"] == "home-budget-ocr-performance"
    assert ocr_dashboard_spec["title"] == "Home Budget OCR Performance"
    assert len(ocr_dashboard_panels) == 8
    assert {
        variable["spec"]["name"] for variable in ocr_dashboard_spec["variables"]
    } == {
        "environment", "service", "worker_host", "engine", "dpi", "psm",
        "variant", "status",
    }
    assert any(
        target.get("exemplar") is True
        for panel in ocr_dashboard_panels
        for target in _v2_panel_targets(panel)
    )
    ocr_idle_zero_panels = {
        "OCR passes / second",
        "OCR failure ratio",
        "Selected OCR bases / second",
    }
    assert all(
        _v2_panel_targets(panel)[0]["expr"].endswith("or vector(0)")
        for panel in ocr_dashboard_panels
        if panel["title"] in ocr_idle_zero_panels
    )

    assert "validate: /usr/bin/promtool check rules %s" in tasks
    assert "validate: /usr/bin/promtool check config %s" in tasks
    assert "- name: Independently scrape Alloy metrics from Prometheus" in tasks
    assert "- job_name: alloy" in tasks
    assert 'http://{{ monitoring_alloy_metrics_target }}/metrics' in tasks
    assert "- name: Wait for Prometheus to scrape Alloy metrics" in tasks
    assert "- name: Read active Prometheus runtime flags" in tasks
    assert (
        "- name: Restart Prometheus when installed receiver flags are not active"
        in tasks
    )
    assert "- name: Confirm Prometheus accepts remote writes and exemplars" in tasks
    assert "- name: Confirm home-budget Prometheus rules are active" in tasks
    assert "tracesToMetrics:" in tasks
    assert "{key: service.name, value: service}" in tasks
    assert "- name: Verify Grafana Prometheus datasource health" in tasks
    assert "- name: Confirm native Grafana v2 dashboards" in tasks
    assert "- name: Read native Grafana folders" in tasks
    assert "monitoring_grafana_folder_list.json['items']" in tasks
    assert "monitoring_grafana_folder_list.json.items" not in tasks
    assert "- name: Create missing native Grafana folders" in tasks
    assert "- name: Update changed native Grafana folders" in tasks
    assert "- name: Verify native Grafana v2 dashboards" in tasks
    assert "/apis/dashboard.grafana.app/v2/namespaces/" in tasks
    assert "/apis/folder.grafana.app/v1/namespaces/" in tasks
    assert "grafana-dashboard-provider.yml.j2" not in tasks
    assert "listen: restart Grafana" in handlers


def test_public_live_demo_is_sanitized_and_revocable():
    defaults = yaml.safe_load(
        (ROOT / "ops/monitoring/roles/monitoring/defaults/main.yml").read_text()
    )
    inventory = yaml.safe_load(
        (ROOT / "ops/monitoring/inventory/group_vars/monitoring.yml").read_text()
    )
    tasks = (ROOT / "ops/monitoring/roles/monitoring/tasks/main.yml").read_text()
    handlers = (ROOT / "ops/monitoring/roles/monitoring/handlers/main.yml").read_text()

    dashboard_text = (
        ROOT
        / "ops/monitoring/roles/monitoring/templates/"
        "grafana-public-live-demo-dashboard.json.j2"
    ).read_text()
    dashboard_text = dashboard_text.replace(
        "{{ monitoring_grafana_prometheus_datasource_uid }}",
        "home-budget-prometheus",
    ).replace(
        "{{ monitoring_grafana_brown_rook_folder_uid_effective }}",
        "brown-rook",
    ).replace(
        "{{ monitoring_grafana_public_demo_dashboard_uid }}",
        "brown-rook-live-telemetry",
    ).replace(
        "{{ monitoring_grafana_public_demo_dashboard_revision }}",
        "kan-119-v2",
    )
    dashboard = json.loads(dashboard_text)

    assert inventory["monitoring_grafana_public_demo_dashboard_uid"] == (
        "brown-rook-live-telemetry"
    )
    assert inventory["monitoring_grafana_public_demo_dashboard_revision"] == (
        "kan-119-v2"
    )
    assert defaults["monitoring_grafana_public_demo_enabled"] is True
    assert defaults["monitoring_grafana_public_demo_annotations_enabled"] is False
    assert defaults["monitoring_grafana_public_demo_time_selection_enabled"] is False
    assert inventory["monitoring_grafana_public_demo_external_url"] == (
        "https://telemetry.idc.brownrook.com"
    )
    assert inventory["monitoring_grafana_public_demo_access_token"] == (
        "08396daff846416b806d71cd965a6c6e"
    )
    assert inventory["monitoring_grafana_public_proxy_ipv4_sources"] == [
        "192.168.2.210/32",
        "192.168.2.230/32",
    ]

    dashboard_spec = _v2_dashboard_spec(dashboard)
    dashboard_panels = _v2_dashboard_panels(dashboard)
    assert dashboard["metadata"]["name"] == "brown-rook-live-telemetry"
    assert dashboard["metadata"]["annotations"]["grafana.app/folder"] == (
        "brown-rook"
    )
    assert dashboard_spec["title"] == "Brown Rook Live Receipt Processing"
    assert len(dashboard_panels) == 8
    assert dashboard_spec["variables"] == []
    assert dashboard_spec["annotations"] == []
    assert dashboard_spec["links"] == []
    assert dashboard_spec["timeSettings"]["autoRefresh"] == "60s"
    assert dashboard_spec["timeSettings"]["from"] == "now-6h"
    assert dashboard_spec["timeSettings"]["to"] == "now"
    assert dashboard_spec["timeSettings"]["hideTimepicker"] is True
    assert dashboard_spec["layout"]["kind"] == "GridLayout"

    assert all(
        query["query"]["datasource"]["name"] == "home-budget-prometheus"
        for panel in dashboard_panels
        for query in _v2_panel_query_models(panel)
    )
    assert all(
        target.get("exemplar") is not True
        for panel in dashboard_panels
        for target in _v2_panel_targets(panel)
    )

    public_expressions = "\n".join(
        target["expr"]
        for panel in dashboard_panels
        for target in _v2_panel_targets(panel)
    ).lower()
    forbidden_query_fields = {
        "instance",
        "service=",
        "worker_host",
        "queue",
        "vhost",
        "rabbitmq",
        "fluentbit",
        "otelcol",
        "alloy",
        "loki",
        "tempo",
        "trace_id",
        "run_uuid",
        "pass_id",
        "receipt_hash",
        "source_reference",
    }
    assert not (forbidden_query_fields & set(public_expressions.split()))
    assert all(field not in public_expressions for field in forbidden_query_fields)

    assert 'Environment="GF_AUTH_ANONYMOUS_ENABLED=false"' in tasks
    assert 'Environment="GF_PUBLIC_DASHBOARDS_ENABLED=true"' in tasks
    assert 'Environment="GF_SERVER_HTTP_ADDR=0.0.0.0"' in tasks
    assert 'Environment="GF_SERVER_ENFORCE_DOMAIN=false"' in tasks
    assert "GF_FEATURE_TOGGLES_dashboardNewLayouts" not in tasks
    assert "tcp dport {{ monitoring_grafana_port }} drop" in (
        ROOT / "ops/monitoring/roles/monitoring/templates/nftables.conf.j2"
    ).read_text()
    assert "daemon_reload: true" in handlers
    assert "- name: Enable external sharing for the Grafana public live demo" in tasks
    assert "- name: Reconcile external sharing for the Grafana public live demo" in tasks
    assert (
        "- name: Verify ordinary Grafana routes are absent from the public hostname"
        in tasks
    )
    public_route_check = tasks.split(
        "- name: Verify the public live-demo route without Grafana credentials",
        maxsplit=1,
    )[1].split("- name:", maxsplit=1)[0]
    assert "follow_redirects: none" in public_route_check
    assert "status_code: 200" in public_route_check
    assert 'url: "{{ monitoring_grafana_public_demo_external_url }}{{ item }}"' in tasks
    assert "status_code: 404" in tasks
    for blocked_path in ("/", "/login", "/explore", "/api/health", "/api/search"):
        assert f"    - {blocked_path}" in tasks
    assert "delegate_to: localhost" in tasks
    assert "annotationsEnabled:" in tasks
    assert "timeSelectionEnabled:" in tasks
    assert "share: public" in tasks


def test_monitoring_grafana_uses_entra_sso_with_local_recovery():
    defaults = yaml.safe_load(
        (ROOT / "ops/monitoring/roles/monitoring/defaults/main.yml").read_text()
    )
    inventory = yaml.safe_load(
        (ROOT / "ops/monitoring/inventory/group_vars/monitoring.yml").read_text()
    )
    tasks = (
        ROOT / "ops/monitoring/roles/monitoring/tasks/main.yml"
    ).read_text()
    wrapper = (ROOT / "scripts/monitoring_gitops.sh").read_text()
    env_example = (ROOT / "ops/monitoring/env.example").read_text()

    assert inventory["monitoring_grafana_url"] == (
        "https://grafana.idc.brownrook.net"
    )
    assert inventory["monitoring_grafana_entra_tenant_id"] == (
        "8b07f4bd-41e4-4106-8d49-00c5d79d35a2"
    )
    assert inventory["monitoring_grafana_entra_client_id"] == (
        "670b17ca-a1e7-4c63-80e8-9d6a21c31fe4"
    )
    assert defaults["monitoring_grafana_entra_environment_path"] == (
        "/etc/grafana/grafana-entra.env"
    )
    assert "MONITORING_GRAFANA_ENTRA_CLIENT_SECRET_FILE" in wrapper
    assert "MONITORING_GRAFANA_ENTRA_CLIENT_SECRET_FILE" in env_example

    secret_task = tasks.split(
        "- name: Install Grafana Entra client secret environment", maxsplit=1
    )[1].split("- name:", maxsplit=1)[0]
    assert 'mode: "0640"' in secret_task
    assert "backup: false" in secret_task
    assert "no_log: true" in secret_task

    assert 'Environment="GF_AUTH_BASIC_ENABLED=true"' in tasks
    assert 'Environment="GF_AUTH_DISABLE_LOGIN_FORM=false"' in tasks
    assert 'Environment="GF_AUTH_AZUREAD_ENABLED=true"' in tasks
    assert 'Environment="GF_AUTH_AZUREAD_ROLE_ATTRIBUTE_STRICT=true"' in tasks
    assert 'Environment="GF_AUTH_AZUREAD_ALLOW_ASSIGN_GRAFANA_ADMIN=true"' in tasks
    assert 'Environment="GF_AUTH_AZUREAD_USE_REFRESH_TOKEN=true"' in tasks
    assert "- name: Probe Grafana Entra login redirect" in tasks
    assert (
        "- name: Confirm Grafana redirects authentication to the Brown Rook Entra tenant"
        in tasks
    )
