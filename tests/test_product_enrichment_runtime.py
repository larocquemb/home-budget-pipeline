from __future__ import annotations

from home_budget_pipeline import product_enrichment_runtime as runtime


def test_brave_search_uses_configured_timeout_and_logs_error(monkeypatch, capsys):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["timeout"] = timeout
        raise TimeoutError("slow search")

    monkeypatch.setenv("BRAVE_TIMEOUT_SECONDS", "7")
    monkeypatch.setattr(runtime.urllib.request, "urlopen", fake_urlopen)

    result = runtime.brave_search(
        "api-key",
        "site:sobeys.com Cep Pic Med",
        "Cep Pic Med",
        "sobeys.com",
    )

    assert result is None
    assert seen["timeout"] == 7.0
    output = capsys.readouterr().out
    assert "enrichment brave_start" in output
    assert "timeout=7s" in output
    assert "enrichment brave_error" in output
    assert "TimeoutError" in output


def test_log_flushes_immediately(monkeypatch):
    calls = []

    def fake_print(message, *, flush):
        calls.append((message, flush))

    monkeypatch.setattr("builtins.print", fake_print)
    runtime._log("progress")

    assert calls == [("progress", True)]
