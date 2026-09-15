"""Non-blocking OpenTelemetry and structured-log support for receipt workers.

The application deliberately owns its business spans and metrics while the
OpenTelemetry Collector owns durable buffering, backend routing, and tail
sampling.  This module keeps telemetry off the receipt-processing critical
path: configuration failures and exporter outages are logged but never raised
to callers.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import metadata
from threading import Lock
from typing import Any, Iterator, Mapping
from uuid import uuid4


LOG = logging.getLogger(__name__)
_TRUTHY = {"1", "true", "yes", "on"}
_run_uuid: ContextVar[str | None] = ContextVar("receipt_run_uuid", default=None)
_pass_id: ContextVar[int | None] = ContextVar("receipt_pass_id", default=None)
_receipt_session: ContextVar["ReceiptTrace | None"] = ContextVar(
    "receipt_trace_session", default=None,
)
_configure_lock = Lock()
_configured_pid: int | None = None
_configuration_failed_pid: int | None = None
_tracer: Any = None
_meter_provider: Any = None
_tracer_provider: Any = None
_receipt_counter: Any = None
_receipt_duration: Any = None
_cache_lookup_counter: Any = None
_ocr_pass_counter: Any = None
_ocr_pass_duration: Any = None
_ocr_selected_counter: Any = None


class _NoopSpan:
    """Small Span-shaped object used before/without SDK configuration."""

    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def set_status(self, status: Any) -> None:
        pass

    def record_exception(self, exception: BaseException) -> None:
        pass

    def end(self, end_time: int | None = None) -> None:
        pass

    def get_span_context(self):
        return None


def _truthy(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in _TRUTHY


def _service_version() -> str:
    configured = os.getenv("OTEL_SERVICE_VERSION", "").strip()
    if configured:
        return configured
    try:
        return metadata.version("home-budget-pipeline")
    except metadata.PackageNotFoundError:
        return "0.1.0"


def resource_attributes() -> dict[str, str]:
    """Return bounded service, host, deployment, and Kubernetes attributes."""
    attributes = {
        "service.name": os.getenv("OTEL_SERVICE_NAME", "home-budget-receipt-worker"),
        "service.version": _service_version(),
        "deployment.environment.name": os.getenv(
            "OTEL_DEPLOYMENT_ENVIRONMENT",
            os.getenv("DEPLOYMENT_ENVIRONMENT", "development"),
        ),
        "host.name": os.getenv("HOSTNAME", socket.gethostname()),
    }
    kubernetes = {
        "k8s.namespace.name": "K8S_NAMESPACE",
        "k8s.pod.name": "K8S_POD_NAME",
        "k8s.pod.uid": "K8S_POD_UID",
        "k8s.node.name": "K8S_NODE_NAME",
        "k8s.container.name": "K8S_CONTAINER_NAME",
    }
    for attribute, variable in kubernetes.items():
        value = os.getenv(variable, "").strip()
        if value:
            attributes[attribute] = value
    return attributes


def configure_telemetry() -> bool:
    """Configure OTLP/gRPC tracing and metrics once for the current process.

    OTLP exporters and BatchSpanProcessor read the standard OTEL_* endpoint,
    TLS, timeout, sampler, and queue variables.  Any initialization error is
    contained here so observability cannot prevent worker startup.
    """
    global _configured_pid, _configuration_failed_pid, _tracer
    global _meter_provider, _tracer_provider
    global _receipt_counter, _receipt_duration, _cache_lookup_counter
    global _ocr_pass_counter, _ocr_pass_duration
    global _ocr_selected_counter

    if not _truthy("HOME_BUDGET_TELEMETRY_ENABLED"):
        return False
    pid = os.getpid()
    if _configured_pid == pid:
        return True
    if _configuration_failed_pid == pid:
        return False
    with _configure_lock:
        if _configured_pid == pid:
            return True
        try:
            from opentelemetry import metrics, trace
            from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
                OTLPMetricExporter,
            )
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            resource = Resource.create(resource_attributes())
            tracer_provider = TracerProvider(resource=resource)
            tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
            trace.set_tracer_provider(tracer_provider)

            interval = int(os.getenv("OTEL_METRIC_EXPORT_INTERVAL", "5000"))
            metric_reader = PeriodicExportingMetricReader(
                OTLPMetricExporter(), export_interval_millis=interval,
            )
            meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
            metrics.set_meter_provider(meter_provider)

            _tracer_provider = tracer_provider
            _meter_provider = meter_provider
            _tracer = trace.get_tracer("home_budget_pipeline.receipts", _service_version())
            meter = metrics.get_meter("home_budget_pipeline.receipts", _service_version())
            _receipt_counter = meter.create_counter(
                "brownrook.receipt.processed",
                unit="{receipt}",
                description="Receipt processing attempts by bounded outcome.",
            )
            _receipt_duration = meter.create_histogram(
                "brownrook.receipt.processing.duration",
                unit="s",
                description="End-to-end receipt processing duration.",
            )
            _cache_lookup_counter = meter.create_counter(
                "brownrook.receipt.cache.lookup",
                unit="{lookup}",
                description="Receipt OCR cache lookups by bounded hit or miss result.",
            )
            _ocr_pass_counter = meter.create_counter(
                "brownrook.receipt.ocr.pass.completed",
                unit="{pass}",
                description="Completed OCR passes by bounded OCR configuration.",
            )
            _ocr_pass_duration = meter.create_histogram(
                "brownrook.receipt.ocr.pass.duration",
                unit="s",
                description="OCR pass duration by bounded OCR configuration.",
            )
            _ocr_selected_counter = meter.create_counter(
                "brownrook.receipt.ocr.pass.selected",
                unit="{pass}",
                description="OCR passes selected as page consensus bases.",
            )
            _configured_pid = pid
            _configuration_failed_pid = None
            return True
        except Exception as exc:  # telemetry must never become a worker dependency
            _configuration_failed_pid = pid
            LOG.warning(
                "OpenTelemetry disabled after configuration failure",
                extra={"error_type": type(exc).__name__},
            )
            return False


def shutdown_telemetry(timeout_millis: int = 5000) -> None:
    """Best-effort flush for normal process shutdown."""
    for provider in (_tracer_provider, _meter_provider):
        if provider is None:
            continue
        try:
            provider.force_flush(timeout_millis=timeout_millis)
        except Exception:
            LOG.warning("OpenTelemetry flush failed", exc_info=True)
    for provider in (_meter_provider, _tracer_provider):
        if provider is None:
            continue
        try:
            provider.shutdown()
        except Exception:
            LOG.warning("OpenTelemetry shutdown failed", exc_info=True)


def _clean_attributes(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    if not attributes:
        return {}
    return {key: value for key, value in attributes.items() if value is not None}


def _context_attributes() -> dict[str, Any]:
    return _clean_attributes({"run_uuid": _run_uuid.get(), "pass_id": _pass_id.get()})


@contextmanager
def span(name: str, attributes: Mapping[str, Any] | None = None) -> Iterator[Any]:
    """Create a current child span, or a harmless placeholder when disabled."""
    combined = _context_attributes()
    combined.update(_clean_attributes(attributes))
    if _tracer is None:
        yield _NoopSpan()
        return
    try:
        with _tracer.start_as_current_span(name, attributes=combined) as current:
            yield current
    except Exception:
        # Exceptions from the instrumented operation must retain their original
        # semantics. SDK/exporter failures are asynchronous and do not arrive
        # here; this merely preserves the normal context-manager behavior.
        raise


@dataclass
class ReceiptTrace:
    run_uuid: str
    span: Any
    started: float
    status: str = "unknown"

    def finish(self, status: str) -> None:
        self.status = status
        self.span.set_attribute("receipt.status", status)

    def adopt_run_uuid(self, run_uuid: str) -> None:
        self.run_uuid = run_uuid
        self.span.set_attribute("run_uuid", run_uuid)
        _run_uuid.set(run_uuid)


@contextmanager
def receipt_process(
    source_reference: str,
    source_sha256: str,
    *,
    run_uuid: str | None = None,
) -> Iterator[ReceiptTrace]:
    """Create the root ``receipt.process`` span and bounded outcome metrics."""
    selected_run_uuid = run_uuid or str(uuid4())
    run_token = _run_uuid.set(selected_run_uuid)
    started = time.perf_counter()
    try:
        with span(
            "receipt.process",
            {
                "run_uuid": selected_run_uuid,
                "receipt.source_reference": source_reference,
                "receipt.source_sha256": source_sha256,
            },
        ) as root:
            session = ReceiptTrace(selected_run_uuid, root, started)
            session_token = _receipt_session.set(session)
            try:
                yield session
            except BaseException as exc:
                session.status = "failed"
                root.set_attribute("receipt.status", "failed")
                root.set_attribute("error.type", type(exc).__name__)
                LOG.error(
                    "Receipt processing failed",
                    extra={"error_type": type(exc).__name__, "status": "failed"},
                )
                _emit_failure_trace(session, source_reference, source_sha256, exc)
                raise
            finally:
                duration = time.perf_counter() - started
                root.set_attribute("receipt.status", session.status)
                root.set_attribute("receipt.processing.duration", duration)
                metric_attributes = {"status": session.status}
                try:
                    if _receipt_counter is not None:
                        _receipt_counter.add(1, metric_attributes)
                    if _receipt_duration is not None:
                        _receipt_duration.record(duration, metric_attributes)
                except Exception:
                    LOG.warning("Receipt telemetry metric recording failed", exc_info=True)
                _receipt_session.reset(session_token)
    finally:
        _run_uuid.reset(run_token)


def current_run_uuid() -> str | None:
    return _run_uuid.get()


def record_cache_lookup(cache_hit: bool) -> None:
    """Record a cache hit or miss without attaching receipt identity."""
    try:
        if _cache_lookup_counter is not None:
            _cache_lookup_counter.add(1, {"result": "hit" if cache_hit else "miss"})
    except Exception:
        LOG.warning("Receipt cache telemetry recording failed", exc_info=True)


def _emit_failure_trace(
    session: ReceiptTrace,
    source_reference: str,
    source_sha256: str,
    error: BaseException,
) -> None:
    """Emit an immediate ERROR trace even after a tail decision on a long run.

    The main receipt trace remains authoritative. This short companion trace
    guarantees that Collector tail sampling sees an ERROR decision after a
    late failure; ``run_uuid`` links it back to the full trace, logs, and DB.
    """
    if _tracer is None:
        return
    failure_span = None
    try:
        from opentelemetry.context import Context
        from opentelemetry.trace import Status, StatusCode

        failure_span = _tracer.start_span(
            "receipt.process.failure",
            context=Context(),
            attributes={
                "run_uuid": session.run_uuid,
                "receipt.source_reference": source_reference,
                "receipt.source_sha256": source_sha256,
                "receipt.status": "failed",
                "error.type": type(error).__name__,
            },
        )
        failure_span.record_exception(error)
        failure_span.set_status(Status(StatusCode.ERROR, type(error).__name__))
    except Exception:
        LOG.warning("Receipt failure trace creation failed", exc_info=True)
    finally:
        if failure_span is not None:
            try:
                failure_span.end()
            except Exception:
                LOG.warning("Receipt failure trace end failed", exc_info=True)


def adopt_run_uuid(run_uuid: str | None) -> None:
    """Replace a provisional trace run UUID with the cache/DB identity."""
    if not run_uuid:
        return
    session = _receipt_session.get()
    if session is not None:
        session.adopt_run_uuid(run_uuid)


def start_ocr_pass(attributes: Mapping[str, Any]) -> Any:
    """Start a pass span now; finish it once page consensus attributes exist."""
    if _tracer is None:
        return _NoopSpan()
    combined = _context_attributes()
    combined.update(_clean_attributes(attributes))
    try:
        return _tracer.start_span(
            "receipt.ocr.pass", attributes=combined, start_time=time.time_ns(),
        )
    except Exception:
        LOG.warning("OCR pass span creation failed", exc_info=True)
        return _NoopSpan()


def start_operation(name: str, attributes: Mapping[str, Any] | None = None) -> Any:
    """Start a manually-ended operation span under the current receipt root."""
    if _tracer is None:
        return _NoopSpan()
    combined = _context_attributes()
    combined.update(_clean_attributes(attributes))
    try:
        return _tracer.start_span(name, attributes=combined, start_time=time.time_ns())
    except Exception:
        LOG.warning("Operation span creation failed", extra={"operation": name}, exc_info=True)
        return _NoopSpan()


def finish_operation(
    operation_span: Any,
    attributes: Mapping[str, Any] | None = None,
    error: BaseException | None = None,
) -> None:
    """Finish a manually-ended span while containing instrumentation errors."""
    try:
        for key, value in _clean_attributes(attributes).items():
            operation_span.set_attribute(key, value)
        if error is not None:
            operation_span.record_exception(error)
            try:
                from opentelemetry.trace import Status, StatusCode

                operation_span.set_status(Status(StatusCode.ERROR, type(error).__name__))
            except ImportError:
                pass
    except Exception:
        LOG.warning("Operation span finalization failed", exc_info=True)
    finally:
        try:
            operation_span.end()
        except Exception:
            LOG.warning("Operation span end failed", exc_info=True)


def _ocr_metric_attributes(metric: Mapping[str, Any]) -> dict[str, Any]:
    """Keep high-cardinality run/pass identities out of metrics."""
    return _clean_attributes({
        "engine": metric.get("engine"),
        "engine_type": metric.get("engine_type"),
        "dpi": metric.get("dpi"),
        "psm": metric.get("psm"),
        "variant": metric.get("variant"),
        "status": metric.get("status"),
    })


def record_ocr_pass_completion(pass_span: Any, metric: Mapping[str, Any]) -> None:
    """Publish pass metrics and a correlated log as soon as the engine returns."""
    try:
        bounded = _ocr_metric_attributes(metric)
        if _ocr_pass_counter is not None:
            _ocr_pass_counter.add(1, bounded)
        if _ocr_pass_duration is not None:
            _ocr_pass_duration.record(float(metric.get("seconds") or 0.0), bounded)
        pass_token = _pass_id.set(metric.get("pass_id"))
        run_token = _run_uuid.set(metric.get("run_uuid") or _run_uuid.get())
        try:
            if _tracer is not None and not isinstance(pass_span, _NoopSpan):
                from opentelemetry import trace

                with trace.use_span(pass_span, end_on_exit=False):
                    LOG.info(
                        "OCR pass completed",
                        extra={
                            "engine": metric.get("engine"),
                            "dpi": metric.get("dpi"),
                            "psm": metric.get("psm"),
                            "variant": metric.get("variant"),
                            "status": metric.get("status"),
                        },
                    )
        finally:
            _run_uuid.reset(run_token)
            _pass_id.reset(pass_token)
    except Exception:
        LOG.warning("OCR pass completion telemetry failed", exc_info=True)


def finish_ocr_pass(
    pass_span: Any,
    metric: Mapping[str, Any],
    end_time_unix_nano: int | None = None,
) -> None:
    """Enrich, log, measure, and end one completed OCR pass."""
    span_attributes = {
        "run_uuid": metric.get("run_uuid"),
        "pass_id": metric.get("pass_id"),
        "receipt.page.number": metric.get("page_number"),
        "ocr.engine": metric.get("engine"),
        "ocr.engine.type": metric.get("engine_type"),
        "ocr.dpi": metric.get("dpi"),
        "ocr.psm": metric.get("psm"),
        "ocr.variant": metric.get("variant"),
        "ocr.status": metric.get("status"),
        "ocr.duration": metric.get("seconds"),
        "ocr.selected_base": bool(metric.get("selected_base")),
        "ocr.structural_score": metric.get("structural_score"),
        "ocr.consensus.line_coverage": metric.get("consensus_line_coverage"),
        "ocr.consensus.coverage_ratio": metric.get("consensus_coverage_ratio"),
        "error.type": metric.get("error_type"),
    }
    try:
        for key, value in _clean_attributes(span_attributes).items():
            pass_span.set_attribute(key, value)
        if metric.get("status") == "failed":
            from opentelemetry.trace import Status, StatusCode

            pass_span.set_status(Status(StatusCode.ERROR, str(metric.get("error_type") or "OCR failed")))
        if bool(metric.get("selected_base")) and _ocr_selected_counter is not None:
            _ocr_selected_counter.add(1, _ocr_metric_attributes(metric))
        pass_token = _pass_id.set(metric.get("pass_id"))
        run_token = _run_uuid.set(metric.get("run_uuid") or _run_uuid.get())
        try:
            if _tracer is not None and not isinstance(pass_span, _NoopSpan):
                from opentelemetry import trace

                with trace.use_span(pass_span, end_on_exit=False):
                    LOG.info(
                        "OCR pass consensus recorded",
                        extra={
                            "engine": metric.get("engine"),
                            "dpi": metric.get("dpi"),
                            "psm": metric.get("psm"),
                            "variant": metric.get("variant"),
                            "status": metric.get("status"),
                            "selected_base": bool(metric.get("selected_base")),
                            "consensus_coverage_ratio": metric.get("consensus_coverage_ratio"),
                        },
                    )
        finally:
            _run_uuid.reset(run_token)
            _pass_id.reset(pass_token)
    except Exception:
        LOG.warning("OCR pass telemetry finalization failed", exc_info=True)
    finally:
        try:
            pass_span.end(end_time=end_time_unix_nano)
        except Exception:
            LOG.warning("OCR pass span end failed", exc_info=True)


def _trace_ids() -> tuple[str | None, str | None]:
    try:
        from opentelemetry import trace

        context = trace.get_current_span().get_span_context()
        if context is None or not context.is_valid:
            return None, None
        return f"{context.trace_id:032x}", f"{context.span_id:016x}"
    except Exception:
        return None, None


class JsonLogFormatter(logging.Formatter):
    """One-line JSON logs with OpenTelemetry and OCR database identities."""

    _extra_fields = (
        "status", "error_type", "engine", "dpi", "psm", "variant",
        "selected_base", "consensus_coverage_ratio", "receipt",
    )

    def format(self, record: logging.LogRecord) -> str:
        trace_id, span_id = _trace_ids()
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "trace_id": trace_id,
            "span_id": span_id,
            "run_uuid": getattr(record, "run_uuid", None) or _run_uuid.get(),
            "pass_id": getattr(record, "pass_id", None) or _pass_id.get(),
        }
        for name in self._extra_fields:
            value = getattr(record, name, None)
            if value is not None:
                payload[name] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(level: int = logging.INFO) -> None:
    """Configure worker logging, opting into JSON when telemetry is enabled."""
    structured = _truthy(
        "HOME_BUDGET_STRUCTURED_LOGS",
        default=_truthy("HOME_BUDGET_TELEMETRY_ENABLED"),
    )
    if structured:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonLogFormatter())
        logging.basicConfig(level=level, handlers=[handler], force=True)
    else:
        logging.basicConfig(level=level)
