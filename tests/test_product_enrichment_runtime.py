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


def test_ai_wrapper_calls_original_function_after_runtime_patch(monkeypatch):
    import openai
    from types import SimpleNamespace

    calls = []
    def original(item, merchant, *, client):
        calls.append((item, merchant))
        return ("expanded product",)

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(openai, "OpenAI", lambda **kw: SimpleNamespace())
    monkeypatch.setattr(runtime, "_core_ai_product_queries", original)
    monkeypatch.setattr(runtime.core, "ai_product_queries", runtime.ai_product_queries)
    assert runtime.core.ai_product_queries("abbreviated", "Sobeys") == ("expanded product",)
    assert calls == [("abbreviated", "Sobeys")]
