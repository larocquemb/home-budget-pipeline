import io
import json
import logging
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml

from home_budget_pipeline import telemetry
from home_budget_pipeline.receipts import evidence, ingest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def in_memory_telemetry(monkeypatch):
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telemetry, "_tracer", provider.get_tracer("test"))
    monkeypatch.setattr(telemetry, "_receipt_counter", None)
    monkeypatch.setattr(telemetry, "_receipt_duration", None)
    monkeypatch.setattr(telemetry, "_ocr_pass_counter", None)
    monkeypatch.setattr(telemetry, "_ocr_pass_duration", None)
    monkeypatch.setattr(telemetry, "_ocr_selected_counter", None)
    yield exporter
    provider.shutdown()


def test_receipt_trace_exports_ocr_before_root_finishes(in_memory_telemetry):
    exporter = in_memory_telemetry
    run_uuid = "11111111-1111-1111-1111-111111111111"
    with telemetry.receipt_process("2026/receipt.pdf", "a" * 64, run_uuid=run_uuid) as receipt:
        with telemetry.span("receipt.cache.lookup"):
            pass
        page = telemetry.start_operation("receipt.page.processing", {"receipt.page.number": 1})
        pass_span = telemetry.start_ocr_pass({"run_uuid": run_uuid, "pass_id": 7})
        telemetry.finish_ocr_pass(pass_span, {
            "run_uuid": run_uuid,
            "pass_id": 7,
            "page_number": 1,
            "engine": "tesseract",
            "engine_type": "traditional_ocr",
            "dpi": 300,
            "psm": "6",
            "variant": "enhanced",
            "seconds": 0.25,
            "status": "success",
            "selected_base": True,
            "structural_score": 8,
            "consensus_line_coverage": 10,
            "consensus_coverage_ratio": 0.91,
        })

        # The in-memory exporter stands in for the Collector/backend boundary:
        # this pass is queryable while receipt.process is still open.
        finished = exporter.get_finished_spans()
        ocr_span = next(item for item in finished if item.name == "receipt.ocr.pass")
        assert all(item.name != "receipt.process" for item in finished)
        assert ocr_span.attributes["run_uuid"] == run_uuid
        assert ocr_span.attributes["pass_id"] == 7
        assert ocr_span.attributes["ocr.selected_base"] is True
        assert ocr_span.attributes["ocr.structural_score"] == 8
        assert ocr_span.attributes["ocr.consensus.coverage_ratio"] == 0.91

        telemetry.finish_operation(page, {"receipt.page.status": "completed"})
        for name in (
            "receipt.consensus.selection",
            "receipt.parsing",
            "receipt.reconciliation",
            "receipt.persistence",
        ):
            with telemetry.span(name):
                pass
        receipt.finish("succeeded")

    spans = exporter.get_finished_spans()
    root = next(item for item in spans if item.name == "receipt.process")
    required = {
        "receipt.cache.lookup",
        "receipt.page.processing",
        "receipt.ocr.pass",
        "receipt.consensus.selection",
        "receipt.parsing",
        "receipt.reconciliation",
        "receipt.persistence",
    }
    assert required <= {item.name for item in spans}
    assert all(
        item.context.trace_id == root.context.trace_id
        for item in spans
        if item.name != "receipt.process.failure"
    )


def test_structured_ocr_log_has_trace_and_database_correlation(in_memory_telemetry):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(telemetry.JsonLogFormatter())
    logger = telemetry.LOG
    previous = (logger.level, logger.propagate, list(logger.handlers))
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    run_uuid = "22222222-2222-2222-2222-222222222222"
    try:
        with telemetry.receipt_process("receipt.pdf", "b" * 64, run_uuid=run_uuid) as receipt:
            pass_span = telemetry.start_ocr_pass({"run_uuid": run_uuid, "pass_id": 3})
            telemetry.finish_ocr_pass(pass_span, {
                "run_uuid": run_uuid,
                "pass_id": 3,
                "engine": "tesseract",
                "engine_type": "traditional_ocr",
                "dpi": 200,
                "psm": "4",
                "variant": "raw",
                "seconds": 0.1,
                "status": "success",
                "selected_base": False,
                "structural_score": 4,
                "consensus_line_coverage": 5,
                "consensus_coverage_ratio": 0.5,
            })
            receipt.finish("succeeded")
    finally:
        logger.level, logger.propagate, logger.handlers = previous

    record = json.loads(stream.getvalue().splitlines()[0])
    assert record["trace_id"] and len(record["trace_id"]) == 32
    assert record["span_id"] and len(record["span_id"]) == 16
    assert record["run_uuid"] == run_uuid
    assert record["pass_id"] == 3


def test_late_failure_emits_independent_error_trace(in_memory_telemetry):
    exporter = in_memory_telemetry
    with pytest.raises(RuntimeError, match="database unavailable"):
        with telemetry.receipt_process(
            "receipt.pdf",
            "d" * 64,
            run_uuid="44444444-4444-4444-4444-444444444444",
        ):
            raise RuntimeError("database unavailable")

    spans = exporter.get_finished_spans()
    root = next(item for item in spans if item.name == "receipt.process")
    failure = next(item for item in spans if item.name == "receipt.process.failure")
    assert failure.context.trace_id != root.context.trace_id
    assert failure.status.status_code.name == "ERROR"
    assert failure.attributes["run_uuid"] == root.attributes["run_uuid"]


def test_trace_pass_identity_matches_persisted_ocr_row(in_memory_telemetry):
    exporter = in_memory_telemetry
    run_uuid = "55555555-5555-5555-5555-555555555555"
    metrics = []
    with telemetry.receipt_process("receipt.pdf", "e" * 64, run_uuid=run_uuid) as receipt_trace:
        candidate, tracked = ingest._run_traced_ocr_pass(
            lambda: ingest.OCRCandidate(
                "TOTAL $10.00",
                (ingest.OCRLine("TOTAL $10.00", 99.0),),
                300,
                "6",
            ),
            metrics,
            run_uuid=run_uuid,
            page=1,
            engine="tesseract",
            dpi=300,
            psm="6",
            variant="raw",
        )
        assert candidate.text == "TOTAL $10.00"
        metrics[0].update({
            "selected_base": True,
            "consensus_line_coverage": 1,
            "consensus_coverage_ratio": 1.0,
        })
        telemetry.finish_ocr_pass(*tracked)
        receipt_trace.finish("succeeded")

    ocr_span = next(
        item for item in exporter.get_finished_spans() if item.name == "receipt.ocr.pass"
    )
    conn = MagicMock()
    receipt = SimpleNamespace(
        source_sha256="e" * 64,
        source_reference="receipt.pdf",
        merchant="Test",
        extraction_status="complete",
        extraction_confidence=1.0,
        worker_host="worker-1",
        worker_pid=1,
        ocr_run={
            "run_uuid": run_uuid,
            "cache_version": 13,
            "processed_at": "2026-09-14T00:00:00+00:00",
            "processing_seconds": 1.0,
            "timings": {},
            "ocr_passes": metrics,
        },
    )
    assert evidence.persist_ocr_run(conn, 9, receipt) == 1
    cursor = conn.cursor.return_value.__enter__.return_value
    pass_insert = next(
        call for call in cursor.execute.call_args_list
        if "INSERT INTO budget.receipt_ocr_passes" in call.args[0]
    )
    assert pass_insert.args[1][0:2] == (
        ocr_span.attributes["run_uuid"],
        ocr_span.attributes["pass_id"],
    )


def test_metric_dimensions_exclude_run_and_pass_identifiers(monkeypatch):
    class Instrument:
        def __init__(self):
            self.calls = []

        def add(self, value, attributes):
            self.calls.append((value, attributes))

        def record(self, value, attributes):
            self.calls.append((value, attributes))

    counter = Instrument()
    duration = Instrument()
    monkeypatch.setattr(telemetry, "_ocr_pass_counter", counter)
    monkeypatch.setattr(telemetry, "_ocr_pass_duration", duration)
    metric = {
        "run_uuid": "33333333-3333-3333-3333-333333333333",
        "pass_id": 11,
        "engine": "paddle",
        "engine_type": "traditional_ocr",
        "dpi": 200,
        "psm": "paddle",
        "variant": "raw",
        "seconds": 1.2,
        "status": "success",
        "selected_base": True,
    }
    telemetry.record_ocr_pass_completion(telemetry._NoopSpan(), metric)
    telemetry.finish_ocr_pass(telemetry._NoopSpan(), metric)
    for _, attributes in counter.calls + duration.calls:
        assert "run_uuid" not in attributes
        assert "pass_id" not in attributes
        assert attributes["engine"] == "paddle"


def test_metric_failure_does_not_change_receipt_outcome(monkeypatch):
    class BrokenInstrument:
        def add(self, value, attributes):
            raise ConnectionError("collector unavailable")

    monkeypatch.setattr(telemetry, "_receipt_counter", BrokenInstrument())
    monkeypatch.setattr(telemetry, "_receipt_duration", None)
    with telemetry.receipt_process("receipt.pdf", "c" * 64) as receipt:
        receipt.finish("succeeded")
    assert receipt.status == "succeeded"


def test_resource_attributes_include_bounded_kubernetes_metadata(monkeypatch):
    monkeypatch.setenv("OTEL_SERVICE_NAME", "receipt-worker")
    monkeypatch.setenv("OTEL_SERVICE_VERSION", "1.2.3")
    monkeypatch.setenv("OTEL_DEPLOYMENT_ENVIRONMENT", "test")
    monkeypatch.setenv("K8S_NAMESPACE", "home-budget")
    monkeypatch.setenv("K8S_POD_NAME", "receipt-worker-abc")
    monkeypatch.setenv("K8S_POD_UID", "pod-uid")
    monkeypatch.setenv("K8S_NODE_NAME", "k3s1")
    attributes = telemetry.resource_attributes()
    assert attributes["service.name"] == "receipt-worker"
    assert attributes["service.version"] == "1.2.3"
    assert attributes["deployment.environment.name"] == "test"
    assert attributes["k8s.namespace.name"] == "home-budget"
    assert attributes["k8s.pod.name"] == "receipt-worker-abc"
    assert attributes["k8s.pod.uid"] == "pod-uid"
    assert attributes["k8s.node.name"] == "k3s1"


def test_telemetry_overlay_has_mtls_resiliency_and_worker_configuration():
    if not shutil.which("kubectl"):
        pytest.skip("kubectl is not installed")
    rendered = subprocess.check_output(
        ["kubectl", "kustomize", str(ROOT / "deploy/rabbitmq-private-telemetry")],
        text=True,
    )
    resources = {
        (item["kind"], item["metadata"]["name"]): item
        for item in yaml.safe_load_all(rendered)
    }
    collector = resources[("Deployment", "otel-collector")]
    assert collector["spec"]["replicas"] == 1
    assert collector["spec"]["strategy"]["type"] == "Recreate"
    image = collector["spec"]["template"]["spec"]["containers"][0]["image"]
    assert image == "otel/opentelemetry-collector-contrib:0.160.0"
    assert resources[("PersistentVolumeClaim", "otel-collector-storage")]

    config_resource = next(
        item for (kind, name), item in resources.items()
        if kind == "ConfigMap" and name.startswith("otel-collector-config-")
    )
    config = config_resource["data"]["collector.yaml"]
    assert "client_ca_file:" in config
    assert "tail_sampling:" in config
    assert "name: retain-errors" in config
    assert "type: status_code" in config
    assert "sampling_percentage: ${env:OTEL_TAIL_SAMPLING_PERCENTAGE}" in config
    assert "storage: file_storage" in config
    assert "queue_size: 2048" in config
    assert "max_elapsed_time: 0s" in config

    worker = resources[("Deployment", "receipt-worker")]["spec"]["template"]["spec"]
    container = next(item for item in worker["containers"] if item["name"] == "receipt-worker")
    env = {item["name"]: item for item in container["env"]}
    assert env["HOME_BUDGET_TELEMETRY_ENABLED"]["value"] == "true"
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"]["value"].startswith("https://")
    assert env["OTEL_TRACES_SAMPLER"]["value"] == "parentbased_always_on"
    assert env["K8S_POD_UID"]["valueFrom"]["fieldRef"]["fieldPath"] == "metadata.uid"
    assert any(item["name"] == "telemetry-client-tls" for item in worker["volumes"])
    assert not any(kind == "Secret" for kind, _ in resources)


def test_project_declares_python_opentelemetry_dependencies():
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "opentelemetry-api>=1.44,<2" in project
    assert "opentelemetry-sdk>=1.44,<2" in project
    assert "opentelemetry-exporter-otlp-proto-grpc>=1.44,<2" in project


def test_telemetry_secret_example_targets_managed_backend():
    resources = list(
        yaml.safe_load_all(
            (ROOT / "deploy/opentelemetry/secrets.example.yaml").read_text()
        )
    )
    backend = next(
        item for item in resources if item["metadata"]["name"] == "otel-backend"
    )

    assert backend["stringData"] == {
        "endpoint": "monitoring.idc.brownrook.net:4317",
        "server-name": "monitoring.idc.brownrook.net",
    }
