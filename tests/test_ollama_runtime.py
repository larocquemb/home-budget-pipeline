import pytest

from home_budget_pipeline.ollama_runtime import request_timeout, thread_options


@pytest.mark.parametrize("override,legacy,expected", [
    (None, None, 180), (None, "240", 240), ("", "240", 240),
    ("300", "180", 300), (" 300.5 ", "180", 300.5),
])
def test_ollama_timeout_override_preserves_legacy_fallback(monkeypatch, override, legacy, expected):
    for key, value in (("OLLAMA_TIMEOUT_SECONDS", override), ("COMPARISON_TIMEOUT_SECONDS", legacy)):
        monkeypatch.delenv(key, raising=False)
        if value is not None:
            monkeypatch.setenv(key, value)
    assert request_timeout() == expected


@pytest.mark.parametrize("value", ["0", "-1", "invalid", "nan", "inf"])
def test_invalid_timeouts_fail_before_inference(monkeypatch, value):
    monkeypatch.setenv("OLLAMA_TIMEOUT_SECONDS", value)
    with pytest.raises(ValueError, match="positive finite"):
        request_timeout()


@pytest.mark.parametrize("value", [None, "", " "])
def test_unconfigured_gpu_clients_keep_server_defaults(monkeypatch, value):
    monkeypatch.delenv("OLLAMA_NUM_THREADS", raising=False)
    if value is not None:
        monkeypatch.setenv("OLLAMA_NUM_THREADS", value)
    assert thread_options() == {}


@pytest.mark.parametrize("value", ["0", "-1", "five", "5.5"])
def test_invalid_thread_counts_fail_before_inference(monkeypatch, value):
    monkeypatch.setenv("OLLAMA_NUM_THREADS", value)
    with pytest.raises(ValueError, match="positive integer"):
        thread_options()


def test_production_cpu_requests_use_five_threads(monkeypatch):
    monkeypatch.setenv("OLLAMA_NUM_THREADS", "5")
    assert thread_options() == {"num_thread": 5}


def test_production_callers_configure_threads_only_for_qwen():
    from pathlib import Path
    import yaml
    root = Path(__file__).resolve().parents[1]
    workers = list(yaml.safe_load_all((root / "k8s/model-collaboration-workers.yaml").read_text()))
    for worker in workers:
        if not worker or worker.get("kind") != "Deployment":
            continue
        env = {row["name"]: row.get("value") for row in worker["spec"]["template"]["spec"]["containers"][0]["env"]}
        assert env.get("OLLAMA_NUM_THREADS") == ("5" if worker["metadata"]["name"] == "model-worker-qwen" else None)
        assert env.get("OLLAMA_TIMEOUT_SECONDS") == ("300" if worker["metadata"]["name"] == "model-worker-qwen" else None)
        assert env["COMPARISON_TIMEOUT_SECONDS"] == "180"
        assert worker["spec"]["template"]["spec"]["terminationGracePeriodSeconds"] > 2 * 300
    cron = yaml.safe_load((root / "k8s/product-enrichment-cronjob.yaml").read_text())
    env = {row["name"]: row.get("value") for row in cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["OLLAMA_NUM_THREADS"] == "5"
    assert env["OLLAMA_TIMEOUT_SECONDS"] == "300"
    assert env["COMPARISON_TIMEOUT_SECONDS"] == "180"
