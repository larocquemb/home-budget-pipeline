import json
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from types import SimpleNamespace

import pytest

from home_budget_pipeline.receipts import queue_ingest as queue
from home_budget_pipeline.receipts.message import InvalidReceiptMessage, ReceiptMessage


MESSAGE = ReceiptMessage("a" * 64, "2026/receipt.pdf")
REPROCESS = replace(MESSAGE, version=2, request_id="740023b1-a078-4914-b074-81bd7129bb75")


@pytest.fixture
def channel(monkeypatch):
    # Unit tests exercise protocol ordering without requiring a running broker
    # or importing the optional-at-runtime AMQP implementation.
    monkeypatch.setitem(sys.modules, "pika", SimpleNamespace(BasicProperties=SimpleNamespace))
    events = []
    return SimpleNamespace(
        events=events,
        basic_publish=lambda **kwargs: events.append(("publish", kwargs)),
        basic_ack=lambda **kwargs: events.append(("ack", kwargs)),
    )


def test_contract_round_trip_and_stable_identity():
    assert ReceiptMessage.from_bytes(MESSAGE.to_bytes()) == MESSAGE
    assert replace(MESSAGE, attempt=2, source_reference="copy.pdf").message_id == MESSAGE.message_id


def test_reprocess_contract_preserves_v1_and_has_request_identity():
    assert set(json.loads(MESSAGE.to_bytes())) == {"version", "source_sha256", "source_reference", "attempt"}
    assert ReceiptMessage.from_bytes(REPROCESS.to_bytes()) == REPROCESS
    assert replace(REPROCESS, attempt=2).message_id == REPROCESS.message_id
    assert replace(REPROCESS, request_id="840023b1-a078-4914-b074-81bd7129bb75").message_id != REPROCESS.message_id


@pytest.mark.parametrize("request_id", [None, "", "not-a-uuid", True, 12, "740023B1-A078-4914-B074-81BD7129BB75"])
def test_reprocess_contract_rejects_invalid_request_id(request_id):
    with pytest.raises(InvalidReceiptMessage):
        replace(REPROCESS, request_id=request_id)


def test_normal_message_cannot_request_reprocessing():
    with pytest.raises(InvalidReceiptMessage):
        replace(MESSAGE, request_id=REPROCESS.request_id)


@pytest.mark.parametrize("field,value", [
    ("version", 2), ("version", True), ("source_sha256", "bad"),
    ("source_reference", "/etc/passwd"), ("source_reference", "../escape.pdf"),
    ("source_reference", "nested/../../escape.pdf"), ("source_reference", "a\\b.pdf"),
    ("source_reference", "a//b.pdf"), ("source_reference", "./a.pdf"),
    ("source_reference", ""), ("source_reference", None),
    ("attempt", 0), ("attempt", 4), ("attempt", True), ("attempt", 1.5),
])
def test_contract_rejects_invalid_values(field, value):
    payload = json.loads(MESSAGE.to_bytes())
    payload[field] = value
    with pytest.raises(InvalidReceiptMessage):
        ReceiptMessage.from_bytes(json.dumps(payload).encode())


@pytest.mark.parametrize("body", [b"not json", b"[]", b"{}", b"\xff", b" " * 8193])
def test_contract_rejects_invalid_body(body):
    with pytest.raises(InvalidReceiptMessage):
        ReceiptMessage.from_bytes(body)


def test_symlink_cannot_escape_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "escape").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(InvalidReceiptMessage):
        replace(MESSAGE, source_reference="escape/outside.pdf").resolve_source(root)


@pytest.mark.parametrize("status", ["succeeded", "review_required", "skipped"])
def test_success_ack_follows_committed_processing(channel, status):
    def process(message):
        channel.events.append(("commit", status))
        return status
    queue.handle_delivery(channel, 42, MESSAGE.to_bytes(), process, queue.Topology())
    assert [event[0] for event in channel.events] == ["commit", "ack"]
    assert channel.events[-1][1] == {"delivery_tag": 42}


def fail(message):
    raise RuntimeError("temporary processing failure")


def test_retry_confirm_precedes_ack(channel):
    queue.handle_delivery(channel, 42, MESSAGE.to_bytes(), fail, queue.Topology())
    publication = channel.events[0][1]
    assert publication["exchange"] == "receipts.v1.retry"
    assert publication["mandatory"] is True
    assert publication["properties"].delivery_mode == 2
    assert publication["properties"].message_id == MESSAGE.message_id
    assert ReceiptMessage.from_bytes(publication["body"]).attempt == 2
    assert [event[0] for event in channel.events] == ["publish", "ack"]


def test_reprocess_retry_preserves_refresh_request_and_message_type(channel):
    queue.handle_delivery(channel, 42, REPROCESS.to_bytes(), fail, queue.Topology())
    publication = channel.events[0][1]
    assert ReceiptMessage.from_bytes(publication["body"]) == replace(REPROCESS, attempt=2)
    assert publication["properties"].message_id == REPROCESS.message_id
    assert publication["properties"].type == "receipt.reprocess.v2"
    assert [event[0] for event in channel.events] == ["publish", "ack"]


@pytest.mark.parametrize("body,processor", [
    (replace(MESSAGE, attempt=3).to_bytes(), fail),
    (b"invalid-json", lambda message: pytest.fail("invalid message was processed")),
    (MESSAGE.to_bytes(), lambda message: (_ for _ in ()).throw(InvalidReceiptMessage("changed source"))),
    (MESSAGE.to_bytes(), lambda message: (_ for _ in ()).throw(queue.backlog.RetryExhausted("exhausted"))),
])
def test_poison_or_exhausted_message_goes_to_dlq(channel, body, processor):
    queue.handle_delivery(channel, 1, body, processor, queue.Topology())
    assert channel.events[0][1]["exchange"] == "receipts.v1.dead"
    assert channel.events[0][1]["body"] == body
    assert channel.events[-1][0] == "ack"


@pytest.mark.parametrize("body", [MESSAGE.to_bytes(), replace(MESSAGE, attempt=3).to_bytes(), b"invalid"])
def test_failed_retry_or_dlq_publish_does_not_ack(channel, body):
    def publish(**kwargs):
        raise ConnectionError("broker connection lost before confirmation")
    channel.basic_publish = publish
    with pytest.raises(ConnectionError):
        queue.handle_delivery(channel, 1, body, fail, queue.Topology())
    assert channel.events == []


def test_heartbeat_io_continues_until_processing_finishes():
    release = Event()
    calls = []
    def process(message):
        assert release.wait(5)
        return "succeeded"
    def io(**kwargs):
        calls.append(kwargs)
        release.set()
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = queue.process_with_heartbeats(SimpleNamespace(process_data_events=io), executor, process, MESSAGE)
    assert result == "succeeded"
    assert calls


def test_topology_has_durable_safe_retry_and_dlq():
    declarations = []
    channel = SimpleNamespace(**{
        name: lambda _name=name, **kwargs: declarations.append((_name, kwargs))
        for name in ("exchange_declare", "queue_declare", "queue_bind", "confirm_delivery")
    })
    queue.declare_topology(channel, queue.Topology())
    queues = {kwargs["queue"]: kwargs for method, kwargs in declarations if method == "queue_declare"}
    for config in queues.values():
        assert config["durable"] is True
        assert config["arguments"]["x-queue-type"] == "quorum"
    retry = queues["receipts.v1.retry"]["arguments"]
    assert retry["x-message-ttl"] == 60000
    assert retry["x-dead-letter-exchange"] == "receipts.v1.work"
    assert retry["x-dead-letter-strategy"] == "at-least-once"
    assert retry["x-overflow"] == "reject-publish"
    assert queues["receipts.v1.work"]["arguments"]["x-dead-letter-exchange"] == "receipts.v1.dead"
    assert declarations[-1][0] == "confirm_delivery"


def test_publisher_skips_completed_exhausted_and_duplicate_sources(tmp_path, monkeypatch):
    from test_receipt_backlog_ingest import FakeConn
    paths = [tmp_path / name for name in ("a.pdf", "copy.pdf", "done.pdf", "exhausted.pdf")]
    candidates = [queue.backlog.ReceiptCandidate(path, sha * 64) for path, sha in zip(paths, "aabc")]
    plan = queue.backlog.DiscoveryPlan(tuple(candidates), tuple(candidates[:2] + candidates[3:]), (candidates[2],))
    monkeypatch.setattr(queue.backlog, "plan_unprocessed_receipts", lambda *args, **kwargs: plan)
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "done.pdf.json").write_text("cached")
    conn = FakeConn(existing=("c" * 64,))
    messages, summary = queue.publication_plan(conn, tmp_path, cache_dir=cache)
    assert messages == [replace(MESSAGE, source_reference="a.pdf")]
    assert summary == {"discovered": 4, "skipped": 1, "exhausted": 1, "cache_missing": 0, "published": 1}


def test_unified_cli_exposes_queue_commands(monkeypatch):
    from home_budget_pipeline.cli import build_parser
    monkeypatch.setenv("RABBITMQ_URL", "amqp://test:test@localhost/")
    for mode in ("publish", "consume"):
        args = build_parser().parse_args(["receipts", mode, "/receipts"])
        assert args.queue_mode == mode
        assert args.handler is queue.run
        assert args.rabbitmq_url == "amqp://test:test@localhost/"


def test_ledger_is_primary_cli_and_brownrook_remains_compatible():
    from home_budget_pipeline.cli import build_parser

    assert build_parser().prog == "ledger"
    assert build_parser("brownrook").prog == "brownrook"


@pytest.mark.parametrize("db_dsn,rabbitmq_url,missing", [
    ("", "amqp://example/", ["database DSN"]),
    ("dbname=test", "", ["RabbitMQ URL"]),
    ("", "", ["database DSN", "RabbitMQ URL"]),
])
def test_missing_configuration_identifies_settings_and_export_fix(db_dsn, rabbitmq_url, missing, monkeypatch):
    monkeypatch.setattr(queue, "connect_broker", lambda *args: pytest.fail("must validate before connecting"))
    monkeypatch.setattr(queue.backlog.scan, "_db_connect", lambda *args: pytest.fail("must validate before connecting"))
    args = SimpleNamespace(
        db_dsn=db_dsn, rabbitmq_url=rabbitmq_url,
        ingest_schema="ingest", budget_schema="budget", queue_mode="publish",
    )
    with pytest.raises(ValueError) as error:
        queue.run(args)
    message = str(error.value)
    for name in ("database DSN", "RabbitMQ URL"):
        assert (name in message) == (name in missing)
    assert "set -a; source .env.dev; set +a" in message
    assert "amqp://example/" not in message
    assert "dbname=test" not in message


def test_queue_cli_suppresses_pika_connection_chatter(monkeypatch):
    logger = queue.logging.getLogger("pika")
    previous_level = logger.level
    args = SimpleNamespace(
        db_dsn="", rabbitmq_url="", ingest_schema="ingest",
        budget_schema="budget", queue_mode="publish",
    )
    try:
        with pytest.raises(ValueError):
            queue.run(args)
        assert logger.level == queue.logging.WARNING
    finally:
        logger.setLevel(previous_level)
