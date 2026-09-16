"""Versioned OCR result events shared by workers and the durable collector.

The contract deliberately contains only query-sized OCR output.  The complete
cache (including page layout and alternate pass text) remains an artifact and
is referenced by URI and digest from the run-completed event.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from uuid import UUID, uuid4

from . import ingest as scan


RESULT_SCHEMA_VERSION = 1
MAX_RESULT_ATTEMPTS = 3
MAX_RESULT_BYTES = 8 * 1024 * 1024
RUN_COMPLETED = "ocr.run-completed.v1"
PASS_COMPLETED = "ocr.pass-completed.v1"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_TRACEPARENT_RE = re.compile(
    r"00-[0-9a-f]{32}-[0-9a-f]{16}-(?:0[01])"
)


class InvalidOcrResultMessage(ValueError):
    """An OCR result does not conform to a supported durable contract."""


def _canonical_uuid(value: object, name: str) -> str:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError("noncanonical UUID")
    except ValueError as exc:
        raise InvalidOcrResultMessage(f"{name} must be a canonical UUID") from exc
    return value


def _source_reference(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise InvalidOcrResultMessage("source_reference must be a relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in value.split("/")):
        raise InvalidOcrResultMessage("source_reference must stay under the receipt root")
    return value


def _timestamp(value: object) -> str:
    if not isinstance(value, str):
        raise InvalidOcrResultMessage("occurred_at must be an ISO 8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidOcrResultMessage("occurred_at must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise InvalidOcrResultMessage("occurred_at must include a timezone")
    return value


def _optional_number(value: object, name: str, *, minimum: float | None = None) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidOcrResultMessage(f"{name} must be numeric or null")
    if minimum is not None and value < minimum:
        raise InvalidOcrResultMessage(f"{name} must be at least {minimum}")


def _optional_text(value: object, name: str) -> None:
    if value is not None and not isinstance(value, str):
        raise InvalidOcrResultMessage(f"{name} must be text or null")


@dataclass(frozen=True)
class ArtifactReference:
    kind: str
    uri: str
    sha256: str
    media_type: str
    size_bytes: int

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind:
            raise InvalidOcrResultMessage("artifact kind is required")
        if not isinstance(self.uri, str) or not self.uri or "://" not in self.uri:
            raise InvalidOcrResultMessage("artifact URI must be absolute")
        if not isinstance(self.sha256, str) or not _SHA256_RE.fullmatch(self.sha256):
            raise InvalidOcrResultMessage("artifact sha256 must be a lowercase SHA-256 digest")
        if not isinstance(self.media_type, str) or "/" not in self.media_type:
            raise InvalidOcrResultMessage("artifact media_type is required")
        if type(self.size_bytes) is not int or self.size_bytes < 0:
            raise InvalidOcrResultMessage("artifact size_bytes must be non-negative")

    @classmethod
    def from_dict(cls, value: object) -> "ArtifactReference":
        fields = {"kind", "uri", "sha256", "media_type", "size_bytes"}
        if not isinstance(value, dict) or set(value) != fields:
            raise InvalidOcrResultMessage("artifact has unexpected fields")
        try:
            return cls(**value)
        except TypeError as exc:
            raise InvalidOcrResultMessage("invalid artifact") from exc


@dataclass(frozen=True)
class OcrResultMessage:
    event_type: str
    run_uuid: str
    pass_id: int | None
    source_sha256: str
    source_reference: str
    worker: dict[str, Any]
    trace_context: dict[str, str]
    occurred_at: str
    payload: dict[str, Any]
    artifacts: tuple[ArtifactReference, ...] = field(default_factory=tuple)
    attempt: int = 1
    schema_version: int = RESULT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != RESULT_SCHEMA_VERSION:
            raise InvalidOcrResultMessage("unsupported OCR result schema version")
        if self.event_type not in {RUN_COMPLETED, PASS_COMPLETED}:
            raise InvalidOcrResultMessage("unsupported OCR result event type")
        _canonical_uuid(self.run_uuid, "run_uuid")
        if not isinstance(self.source_sha256, str) or not _SHA256_RE.fullmatch(self.source_sha256):
            raise InvalidOcrResultMessage("source_sha256 must be a lowercase SHA-256 digest")
        _source_reference(self.source_reference)
        _timestamp(self.occurred_at)
        if type(self.attempt) is not int or not 1 <= self.attempt <= MAX_RESULT_ATTEMPTS:
            raise InvalidOcrResultMessage("invalid result attempt number")
        if not isinstance(self.payload, dict):
            raise InvalidOcrResultMessage("payload must be an object")
        if not isinstance(self.artifacts, tuple) or not all(
            isinstance(item, ArtifactReference) for item in self.artifacts
        ):
            raise InvalidOcrResultMessage("artifacts must contain artifact references")

        if not isinstance(self.worker, dict) or set(self.worker) != {"service", "host", "pid"}:
            raise InvalidOcrResultMessage("worker must contain service, host, and pid")
        if not all(isinstance(self.worker.get(key), str) and self.worker[key] for key in ("service", "host")):
            raise InvalidOcrResultMessage("worker service and host are required")
        if type(self.worker.get("pid")) is not int or self.worker["pid"] <= 0:
            raise InvalidOcrResultMessage("worker pid must be positive")

        if not isinstance(self.trace_context, dict) or "traceparent" not in self.trace_context:
            raise InvalidOcrResultMessage("trace_context.traceparent is required")
        if set(self.trace_context) - {"traceparent", "tracestate"}:
            raise InvalidOcrResultMessage("trace_context has unexpected fields")
        if not isinstance(self.trace_context["traceparent"], str) or not _TRACEPARENT_RE.fullmatch(
            self.trace_context["traceparent"]
        ):
            raise InvalidOcrResultMessage("trace_context.traceparent is invalid")
        _, trace_id, span_id, _ = self.trace_context["traceparent"].split("-")
        if trace_id == "0" * 32 or span_id == "0" * 16:
            raise InvalidOcrResultMessage("trace_context.traceparent contains a zero identifier")
        if "tracestate" in self.trace_context and not isinstance(self.trace_context["tracestate"], str):
            raise InvalidOcrResultMessage("trace_context.tracestate must be text")

        if self.event_type == RUN_COMPLETED:
            if self.pass_id is not None:
                raise InvalidOcrResultMessage("run-completed pass_id must be null")
            required = {"receipt", "run", "persistence_mode", "request_id"}
            if set(self.payload) != required:
                raise InvalidOcrResultMessage("run-completed payload has unexpected fields")
            if self.payload["persistence_mode"] not in {"normal", "reprocess", "cache_only"}:
                raise InvalidOcrResultMessage("invalid persistence_mode")
            request_id = self.payload["request_id"]
            if request_id is not None:
                _canonical_uuid(request_id, "request_id")
            self._validate_run_payload()
        else:
            if type(self.pass_id) is not int or self.pass_id <= 0:
                raise InvalidOcrResultMessage("pass-completed pass_id must be positive")
            if set(self.payload) != {"pass"} or not isinstance(self.payload["pass"], dict):
                raise InvalidOcrResultMessage("pass-completed payload must contain one pass object")
            metric = self.payload["pass"]
            required = {
                "schema_version", "run_uuid", "pass_id", "page_number", "engine",
                "engine_type", "dpi", "psm", "variant", "seconds", "status",
                "error_type", "line_count", "character_count", "structural_score",
                "summary_score", "valid_timestamp", "selected_base",
                "consensus_line_coverage", "consensus_coverage_ratio", "quality",
                "engine_options", "usage", "provenance",
            }
            if set(metric) != required:
                raise InvalidOcrResultMessage("pass-completed payload has unexpected fields")
            if type(metric["schema_version"]) is not int or metric["schema_version"] != RESULT_SCHEMA_VERSION:
                raise InvalidOcrResultMessage("unsupported pass schema version")
            if type(metric["pass_id"]) is not int:
                raise InvalidOcrResultMessage("pass payload pass_id must be an integer")
            if metric["run_uuid"] != self.run_uuid or metric["pass_id"] != self.pass_id:
                raise InvalidOcrResultMessage("pass identity does not match its envelope")
            self._validate_pass_payload(metric)

    def _validate_run_payload(self) -> None:
        receipt = self.payload["receipt"]
        run = self.payload["run"]
        if not isinstance(receipt, dict) or not isinstance(run, dict):
            raise InvalidOcrResultMessage("run and receipt payloads must be objects")
        required_receipt = {
            "merchant", "transaction_date", "transaction_datetime", "receipt_id",
            "subtotal", "tax", "total", "payment_method", "card_last4", "payer",
            "items", "text", "extraction_confidence", "extraction_status", "review_reasons",
        }
        if set(receipt) != required_receipt:
            raise InvalidOcrResultMessage("receipt payload has unexpected fields")
        if not isinstance(receipt["items"], list) or not all(
            isinstance(item, dict) and set(item) == {"item_name", "line_total"}
            for item in receipt["items"]
        ):
            raise InvalidOcrResultMessage("receipt items are invalid")
        for item in receipt["items"]:
            if not isinstance(item["item_name"], str) or not item["item_name"]:
                raise InvalidOcrResultMessage("receipt item_name is required")
            _optional_number(item["line_total"], "receipt item line_total")
        if not isinstance(receipt["text"], str):
            raise InvalidOcrResultMessage("receipt final text must be text")
        if not isinstance(receipt["review_reasons"], list) or not all(
            isinstance(value, str) for value in receipt["review_reasons"]
        ):
            raise InvalidOcrResultMessage("receipt review_reasons are invalid")
        for name in (
            "merchant", "transaction_date", "transaction_datetime", "receipt_id",
            "payment_method", "card_last4", "payer",
        ):
            _optional_text(receipt[name], f"receipt {name}")
        if receipt["transaction_date"] is not None:
            try:
                date.fromisoformat(receipt["transaction_date"])
            except ValueError as exc:
                raise InvalidOcrResultMessage("receipt transaction_date is invalid") from exc
        if receipt["transaction_datetime"] is not None:
            try:
                datetime.fromisoformat(receipt["transaction_datetime"])
            except ValueError as exc:
                raise InvalidOcrResultMessage("receipt transaction_datetime is invalid") from exc
        if receipt["card_last4"] is not None and not re.fullmatch(r"[0-9]{4}", receipt["card_last4"]):
            raise InvalidOcrResultMessage("receipt card_last4 is invalid")
        for name in ("subtotal", "tax", "total"):
            _optional_number(receipt[name], f"receipt {name}")
        _optional_number(receipt["extraction_confidence"], "receipt extraction_confidence", minimum=0)
        if receipt["extraction_confidence"] is not None and receipt["extraction_confidence"] > 1:
            raise InvalidOcrResultMessage("receipt extraction_confidence must not exceed 1")
        if receipt["extraction_status"] not in {"complete", "review", "unreadable"}:
            raise InvalidOcrResultMessage("receipt extraction_status is invalid")
        required_run = {"cache_version", "processed_at", "processing_seconds", "timings"}
        if set(run) != required_run or not isinstance(run["timings"], dict):
            raise InvalidOcrResultMessage("run payload has unexpected fields")
        if type(run["cache_version"]) is not int or run["cache_version"] <= 0:
            raise InvalidOcrResultMessage("run cache_version must be positive")
        _timestamp(run["processed_at"])
        _optional_number(run["processing_seconds"], "run processing_seconds", minimum=0)
        if not all(
            isinstance(key, str)
            and not isinstance(value, bool)
            and isinstance(value, (int, float))
            and value >= 0
            for key, value in run["timings"].items()
        ):
            raise InvalidOcrResultMessage("run timings must contain non-negative numbers")

    @staticmethod
    def _validate_pass_payload(metric: dict[str, Any]) -> None:
        for name in ("engine", "engine_type", "variant", "status"):
            if not isinstance(metric[name], str) or not metric[name]:
                raise InvalidOcrResultMessage(f"pass {name} is required")
        for name in ("psm", "error_type"):
            _optional_text(metric[name], f"pass {name}")
        for name in (
            "page_number", "dpi", "line_count", "character_count", "structural_score",
            "summary_score", "consensus_line_coverage",
        ):
            value = metric[name]
            if value is not None and (type(value) is not int or value < 0):
                raise InvalidOcrResultMessage(f"pass {name} must be a non-negative integer or null")
        _optional_number(metric["seconds"], "pass seconds", minimum=0)
        _optional_number(
            metric["consensus_coverage_ratio"],
            "pass consensus_coverage_ratio",
            minimum=0,
        )
        if metric["consensus_coverage_ratio"] is not None and metric["consensus_coverage_ratio"] > 1:
            raise InvalidOcrResultMessage("pass consensus_coverage_ratio must not exceed 1")
        if metric["valid_timestamp"] is not None and type(metric["valid_timestamp"]) is not bool:
            raise InvalidOcrResultMessage("pass valid_timestamp must be boolean or null")
        if type(metric["selected_base"]) is not bool:
            raise InvalidOcrResultMessage("pass selected_base must be boolean")
        for name in ("quality", "engine_options", "usage", "provenance"):
            if not isinstance(metric[name], dict):
                raise InvalidOcrResultMessage(f"pass {name} must be an object")

    @property
    def message_id(self) -> str:
        suffix = "run" if self.pass_id is None else f"pass:{self.pass_id}"
        return f"ocr-result.v{self.schema_version}:{self.run_uuid}:{suffix}"

    def to_bytes(self) -> bytes:
        value = asdict(self)
        value["artifacts"] = [asdict(item) for item in self.artifacts]
        try:
            body = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise InvalidOcrResultMessage("result message is not JSON serializable") from exc
        if len(body) > MAX_RESULT_BYTES:
            raise InvalidOcrResultMessage(f"result message exceeds {MAX_RESULT_BYTES} bytes")
        return body

    @classmethod
    def from_bytes(cls, body: bytes) -> "OcrResultMessage":
        try:
            if len(body) > MAX_RESULT_BYTES:
                raise ValueError(f"result message exceeds {MAX_RESULT_BYTES} bytes")
            value = json.loads(body)
            fields = {
                "schema_version", "event_type", "run_uuid", "pass_id",
                "source_sha256", "source_reference", "worker", "trace_context",
                "occurred_at", "payload", "artifacts", "attempt",
            }
            if not isinstance(value, dict) or set(value) != fields:
                raise ValueError("unexpected result message fields")
            artifacts = value.pop("artifacts")
            if not isinstance(artifacts, list):
                raise ValueError("artifacts must be an array")
            value["artifacts"] = tuple(ArtifactReference.from_dict(item) for item in artifacts)
            return cls(**value)
        except (ValueError, TypeError, UnicodeError) as exc:
            if isinstance(exc, InvalidOcrResultMessage):
                raise
            raise InvalidOcrResultMessage(str(exc)) from exc

    def next_attempt(self) -> "OcrResultMessage":
        if self.attempt >= MAX_RESULT_ATTEMPTS:
            raise InvalidOcrResultMessage("result retry budget exhausted")
        value = {**self.__dict__, "attempt": self.attempt + 1}
        return type(self)(**value)


def current_trace_context() -> dict[str, str]:
    """Capture W3C context, creating a valid correlation root when OTEL is off."""
    try:
        from opentelemetry import propagate, trace

        carrier: dict[str, str] = {}
        propagate.inject(carrier)
        if _TRACEPARENT_RE.fullmatch(carrier.get("traceparent", "")):
            return {
                key: carrier[key]
                for key in ("traceparent", "tracestate")
                if key in carrier
            }
        context = trace.get_current_span().get_span_context()
        if context and context.is_valid:
            return {
                "traceparent": (
                    f"00-{context.trace_id:032x}-{context.span_id:016x}-"
                    f"{'01' if context.trace_flags.sampled else '00'}"
                )
            }
    except Exception:
        pass
    return faked_trace_context()


def faked_trace_context() -> dict[str, str]:
    """Return a standalone valid W3C context for uninstrumented execution."""
    trace_id = uuid4().hex
    span_id = uuid4().hex[:16]
    return {"traceparent": f"00-{trace_id}-{span_id}-00"}


def worker_identity() -> dict[str, Any]:
    return {
        "service": os.getenv("OTEL_SERVICE_NAME", "home-budget-receipt-worker"),
        "host": os.getenv("HOSTNAME", socket.gethostname()),
        "pid": os.getpid(),
    }


def artifact_reference(
    path: Path,
    cache_root: Path,
    *,
    uri_prefix: str | None = None,
) -> ArtifactReference | None:
    """Persist/describe a completed cache artifact without embedding it in RabbitMQ.

    When ``OCR_ARTIFACT_S3_BUCKET`` is configured, the worker uploads to an
    S3-compatible object store before publishing the result.  Otherwise the
    durable shared-PVC URI is recorded.
    """
    try:
        data = path.read_bytes()
    except (FileNotFoundError, OSError):
        return None
    try:
        relative = path.resolve().relative_to(cache_root.resolve()).as_posix()
    except ValueError:
        relative = path.name
    digest = hashlib.sha256(data).hexdigest()
    bucket = os.getenv("OCR_ARTIFACT_S3_BUCKET", "").strip()
    if bucket:
        key_prefix = os.getenv("OCR_ARTIFACT_S3_PREFIX", "ocr-results").strip("/")
        key = "/".join(part for part in (key_prefix, relative) if part)
        try:
            import boto3
        except ImportError as exc:
            raise RuntimeError(
                "object storage is configured but the object-storage dependency is unavailable"
            ) from exc
        client_options = {
            "service_name": "s3",
            "endpoint_url": os.getenv("OCR_ARTIFACT_S3_ENDPOINT") or None,
            "region_name": os.getenv("OCR_ARTIFACT_S3_REGION") or None,
        }
        client = boto3.client(**client_options)
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=data,
            ContentType="application/json",
            Metadata={"sha256": digest},
        )
        uri = f"s3://{bucket}/{key}"
    else:
        prefix = (uri_prefix or os.getenv("OCR_ARTIFACT_URI_PREFIX", "pvc://receipt-ocr")).rstrip("/")
        uri = f"{prefix}/{relative}"
    return ArtifactReference(
        kind="ocr-cache",
        uri=uri,
        sha256=digest,
        media_type="application/json",
        size_bytes=len(data),
    )


def _receipt_payload(receipt: scan.ScannedReceipt) -> dict[str, Any]:
    return {
        "merchant": receipt.merchant,
        "transaction_date": receipt.transaction_date,
        "transaction_datetime": getattr(receipt, "transaction_datetime", None),
        "receipt_id": receipt.receipt_id,
        "subtotal": receipt.subtotal,
        "tax": receipt.tax,
        "total": receipt.total,
        "payment_method": receipt.payment_method,
        "card_last4": receipt.card_last4,
        "payer": receipt.payer,
        "items": [asdict(item) for item in receipt.items],
        "text": receipt.text,
        "extraction_confidence": receipt.extraction_confidence,
        "extraction_status": receipt.extraction_status,
        "review_reasons": list(receipt.review_reasons),
    }


def receipt_from_message(message: OcrResultMessage) -> scan.ScannedReceipt:
    """Rehydrate the query-sized receipt result for collector persistence."""
    if message.event_type != RUN_COMPLETED:
        raise InvalidOcrResultMessage("only run-completed messages contain a receipt")
    payload = message.payload["receipt"]
    receipt = scan.ScannedReceipt(
        path=message.source_reference,
        source_reference=message.source_reference,
        source_sha256=message.source_sha256,
        merchant=payload["merchant"],
        transaction_date=payload["transaction_date"],
        receipt_id=payload["receipt_id"],
        subtotal=payload["subtotal"],
        tax=payload["tax"],
        total=payload["total"],
        payment_method=payload["payment_method"],
        card_last4=payload["card_last4"],
        payer=payload["payer"],
        items=[scan.ScannedItem(**item) for item in payload["items"]],
        text=payload["text"],
        extraction_confidence=payload["extraction_confidence"],
        extraction_status=payload["extraction_status"],
        review_reasons=payload["review_reasons"],
    )
    receipt.transaction_datetime = payload["transaction_datetime"]
    receipt.worker_host = message.worker["host"]
    receipt.worker_pid = message.worker["pid"]
    receipt.ocr_run = {
        "run_uuid": message.run_uuid,
        **message.payload["run"],
        "ocr_passes": [],
    }
    return receipt


def result_messages_for_receipt(
    receipt: scan.ScannedReceipt,
    *,
    cache_path: Path | None = None,
    cache_root: Path | None = None,
    persistence_mode: str = "normal",
    request_id: str | None = None,
    trace_context: Mapping[str, str] | None = None,
) -> list[OcrResultMessage]:
    """Build one run event followed by idempotent pass events."""
    run = getattr(receipt, "ocr_run", None) or {}
    run_uuid = _canonical_uuid(run.get("run_uuid"), "run_uuid")
    occurred_at = datetime.now(timezone.utc).isoformat()
    context = dict(trace_context or current_trace_context())
    worker = worker_identity()
    artifacts: tuple[ArtifactReference, ...] = ()
    if cache_path is not None and cache_root is not None:
        reference = artifact_reference(cache_path, cache_root)
        if reference is not None:
            artifacts = (reference,)
    run_payload = {
        "cache_version": run.get("cache_version") or 1,
        "processed_at": run.get("processed_at") or occurred_at,
        "processing_seconds": run.get("processing_seconds"),
        "timings": run.get("timings") or {},
    }
    events = [OcrResultMessage(
        event_type=RUN_COMPLETED,
        run_uuid=run_uuid,
        pass_id=None,
        source_sha256=receipt.source_sha256,
        source_reference=receipt.source_reference,
        worker=worker,
        trace_context=context,
        occurred_at=occurred_at,
        payload={
            "receipt": _receipt_payload(receipt),
            "run": run_payload,
            "persistence_mode": persistence_mode,
            "request_id": request_id,
        },
        artifacts=artifacts,
    )]
    for metric in run.get("ocr_passes") or []:
        # Alternate full text lives in the referenced cache.  The selected final
        # text remains in the run-completed receipt payload for review queries.
        pass_payload = {key: value for key, value in metric.items() if key != "text"}
        events.append(OcrResultMessage(
            event_type=PASS_COMPLETED,
            run_uuid=run_uuid,
            pass_id=metric.get("pass_id"),
            source_sha256=receipt.source_sha256,
            source_reference=receipt.source_reference,
            worker=worker,
            trace_context=context,
            occurred_at=occurred_at,
            payload={"pass": pass_payload},
        ))
    return events
