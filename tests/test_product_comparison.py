import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import Environment, StrictUndefined

from home_budget_pipeline import product_comparison as comparison
from home_budget_pipeline import product_enrichment as core

pytestmark = pytest.mark.usefixtures('offline_gpu_sampling')


def test_queries_are_bounded_and_deduplicated():
    assert comparison.parse_queries('{"queries":["Yard bags","Yard bags"]}') == ["Yard bags"]
    for payload in ({"queries": [1]}, {"queries": ["a"] * 4}, {"queries": "a"}):
        with pytest.raises(ValueError):
            comparison.parse_queries(json.dumps(payload))


def test_both_models_share_prompt_search_and_original_item_scoring(monkeypatch):
    prompts, searches = [], []
    def generate(provider, text):
        prompts.append((provider, text))
        return {"queries": ["Kraft yard bags"], "model": provider}
    def search(key, query, item, domain):
        searches.append((query, item, domain))
        return [core.SearchResult("Kraft 5PK Yard Bag", "https://homedepot.ca/product/bags", "", .95)]
    monkeypatch.setattr(comparison, "generate", generate)
    monkeypatch.setattr(core, "brave_candidates", search)
    cache = {}
    results = [comparison.evaluate(p, "5PK YARD BAG", "Home Depot", "secret", .85, cache)
               for p in ("openai", "qwen")]
    assert prompts[0][1] == prompts[1][1]
    assert searches == [("site:homedepot.ca Kraft yard bags", "5PK YARD BAG", "homedepot.ca")]
    assert all(r["accepted"] for r in results)
    assert comparison.compare(results) == {"winner": "tie", "confidence_delta": 0., "agreement": True}


def test_failures_are_not_zero_scores_or_wins_and_secrets_are_not_logged(monkeypatch):
    def fail(*args):
        raise TimeoutError("api-key-secret")
    monkeypatch.setattr(comparison, "generate", fail)
    error = comparison.evaluate("qwen", "a", "Sobeys", "secret", .85, {})
    assert error["confidence"] is None
    assert error["error_type"] == "TimeoutError"
    assert "api-key-secret" not in json.dumps(error)
    success = {"status": "matched", "confidence": .95, "candidate_url": "https://sobeys.com/products/a"}
    assert comparison.compare([success, error])["winner"] == "incomplete"


def test_search_failure_marks_provider_incomplete(monkeypatch):
    monkeypatch.setattr(comparison, "generate", lambda *a: {"queries": ["Yard bags"]})
    def fail(*args):
        raise TimeoutError()
    monkeypatch.setattr(core, "brave_candidates", fail)
    assert comparison.evaluate("openai", "Yard bags", "Home Depot", "key", .85, {})["status"] == "error"


def test_unmatched_is_complete_and_different_products_can_tie(monkeypatch):
    monkeypatch.setattr(comparison, "generate", lambda *a: {"queries": ["Yard bags"]})
    monkeypatch.setattr(core, "brave_candidates", lambda *a: [])
    result = comparison.evaluate("qwen", "Yard bags", "Home Depot", "key", .85, {})
    assert result["status"] == "no_match"
    assert result["confidence"] == 0
    assert comparison.compare([result, result])["agreement"] is False
    left = {"status": "matched", "confidence": .95, "candidate_url": "https://sobeys.com/products/a"}
    right = {**left, "candidate_url": "https://sobeys.com/products/b"}
    assert comparison.compare([left, right]) == {"winner": "tie", "confidence_delta": 0., "agreement": False}


@pytest.mark.parametrize("provider", ["openai", "qwen"])
def test_empty_model_response_retries_once_with_shared_policy(monkeypatch, provider):
    calls = []
    def generate_once(selected, text):
        calls.append((selected, text))
        return {"queries": [] if len(calls) == 1 else ["Expanded grocery product"],
                "model": selected, "input_tokens": 10, "output_tokens": 6}
    monkeypatch.setattr(comparison, "_generate_once", generate_once)
    initial = comparison.prompt("Cep Pic Med", "Sobeys")
    result = comparison.generate(provider, initial)
    assert result["queries"] == ["Expanded grocery product"]
    assert result["attempts"] == 2
    assert result["input_tokens"] == 20 and result["output_tokens"] == 12
    assert calls[0] == (provider, initial)
    assert "previous response contained no queries" in calls[1][1]
    assert comparison.QUERY_SCHEMA["properties"]["queries"]["minItems"] == 1


def test_repeated_empty_queries_are_an_incomplete_error_not_a_zero_match(monkeypatch):
    calls = []
    def generate_once(provider, text):
        calls.append(text)
        return {"queries": [], "model": "qwen3:30b", "input_tokens": 10,
                "output_tokens": 6}
    monkeypatch.setattr(comparison, "_generate_once", generate_once)
    def forbidden_search(*args):
        pytest.fail("No search should run without model proposals")
    monkeypatch.setattr(core, "brave_candidates", forbidden_search)
    result = comparison.evaluate("qwen", "Cep Pic Med", "Sobeys", "key", .85, {})
    assert len(calls) == 2
    assert result["status"] == "error" and result["confidence"] is None
    assert result["error_type"] == "NoProductQueriesError" and result["attempts"] == 2
    assert result["queries"] == [] and result["output_tokens"] == 12
    success = {"status": "matched", "confidence": .5, "candidate_url": "https://sobeys.com/products/a"}
    assert comparison.compare([success, result])["winner"] == "incomplete"


def test_ollama_structured_chat_observes_gpu_memory(monkeypatch):
    calls = []
    class Response:
        def __init__(self, data): self.data = data
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return json.dumps(self.data).encode()
    def urlopen(request, timeout):
        calls.append((request, timeout))
        if isinstance(request, str):
            return Response({"models": [{"name": "qwen3:30b", "size_vram": 18000000000}]})
        return Response({"message": {"content": '{"queries":["Yard bags"]}'},
                         "prompt_eval_count": 42, "eval_count": 12,
                         "total_duration": 3000000000, "load_duration": 1000000000,
                         "prompt_eval_duration": 500000000, "eval_duration": 1500000000})
    monkeypatch.setattr(comparison.urllib.request, "urlopen", urlopen)
    result = comparison.generate("qwen", "same prompt")
    request = json.loads(calls[0][0].data)
    assert request["format"] == comparison.QUERY_SCHEMA
    assert request["messages"][0]["content"] == "same prompt"
    assert request["stream"] is False and request["think"] is False
    assert result["gpu_vram_bytes"] == 18000000000
    assert result["input_tokens"] == 42
    assert request["options"]["num_ctx"] == 4096
    assert result["ollama_load_seconds"] == 1
    assert result["ollama_eval_seconds"] == 1.5


def test_openai_uses_same_schema_and_reports_usage(monkeypatch):
    import openai
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(output_text='{"queries":["Yard bags"]}',
                               usage=SimpleNamespace(input_tokens=22, output_tokens=11))
    monkeypatch.setattr(openai, "OpenAI", lambda **kw: SimpleNamespace(responses=SimpleNamespace(create=create)))
    result = comparison.generate("openai", "same prompt")
    assert calls[0]["text"]["format"]["schema"] == comparison.QUERY_SCHEMA
    assert calls[0]["input"] == "same prompt"
    assert result["output_tokens"] == 11


@pytest.mark.parametrize('receipt_item_count', [1, 3])
def test_run_persists_comparisons_only_and_emits_paired_events(monkeypatch, capsys, receipt_item_count, mock_brave_query_cache):
    import psycopg
    calls = []
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def commit(self): pass
        def execute(self, sql, params):
            calls.append((sql, params))
            return self
        def fetchall(self):
            return [{"id": 42, "item_name": "Yard bags", "store_name": "Home Depot",
                     "receipt_filename": "receipt.pdf", "source_reference": None,
                     "receipt_id": 7, "receipt_item_count": receipt_item_count}]
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setattr(psycopg, "connect", lambda *a, **kw: Connection())
    monkeypatch.setattr(core, "brave_candidates", lambda *a: [])
    monkeypatch.setattr(comparison, "evaluate", lambda provider, *a: {
        "provider": provider, "status": "matched", "confidence": .95,
        "accepted": True, "candidate_url": "https://homedepot.ca/product/bags", "seconds": 1})
    stats = comparison.run(dsn="unused", api_key="unused", limit=1, threshold=.85, write_db=True)
    assert stats["compared"] == 1 and stats["incomplete"] == 0
    mutations = [sql for sql, _ in calls if not sql.lstrip().startswith("SELECT")]
    assert len(mutations) == 2 and "INSERT INTO enrichment.product_comparisons" in mutations[0]
    assert "INSERT INTO enrichment.product_comparison_runs" in mutations[1]
    assert stats["receipts"][0]["complete_receipt"] is (receipt_item_count == 1)
    assert stats["receipts"][0]["selected_items"] == 1
    assert stats["receipts"][0]["openai_seconds"] == 1
    assert stats["receipts"][0]["qwen_seconds"] == 1
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [e["event"] for e in events] == [
        "enrichment_comparison_started", "enrichment_brave_baseline", "enrichment_model_started", "enrichment_model_result",
        "enrichment_model_started", "enrichment_model_result", "enrichment_model_comparison",
        "enrichment_receipt_timing"]
    assert all(e["run_uuid"] == stats["run_uuid"] for e in events)


def test_dashboard_preserves_raw_json_and_unwraps_message_json():
    root = Path(__file__).resolve().parents[1]
    template = (root / "ops/monitoring/roles/monitoring/templates/grafana-product-comparison-dashboard.json.j2").read_text()
    rendered = Environment(undefined=StrictUndefined).from_string(template).render(
        monitoring_grafana_home_budget_folder_uid_effective="home-budget",
        monitoring_grafana_datasource_uid="loki")
    dashboard = json.loads(rendered)
    assert dashboard["metadata"]["annotations"]["grafana.app/folder"] == "home-budget"
    panels = dashboard["spec"]["elements"]
    expressions = [p["spec"]["data"]["spec"]["queries"][0]["spec"]["query"]["spec"]["expr"] for p in panels.values()]
    assert all('| line_format "{{if .message}}{{.message}}{{else}}{{ __line__ }}{{end}}" | json' in expr
               for expr in expressions)
    # Fluent Bit strips metadata and emits the single message value directly.
    collector = (root / "deploy/fluent-bit/fluent-bit.yaml").read_text()
    assert "Drop_Single_Key            raw" in collector
    assert 'status!="error"' in expressions[0]
    assert 'winner!="incomplete"' in expressions[2]
    assert 'gpu_vram_bytes' in expressions[5]


def test_brave_precedes_models_and_both_receive_same_baseline(monkeypatch):
    order, prompts = [], []
    title = 'Old El Paso Salsa Picante Medium'
    url = 'https://sobeys.com/products/old-el-paso-salsa-picante-medium'
    result = core.SearchResult(title, url, '650 ml salsa', core.candidate_score('Cep Pic Med', 'sobeys.com', title, url))
    def search(key, query, item, domain):
        order.append(('brave', query))
        return [result]
    def generate(provider, text):
        order.append(('model', provider))
        prompts.append(text)
        return {'queries': ['Old El Paso Salsa Picante Medium']}
    monkeypatch.setattr(core, 'brave_candidates', search)
    monkeypatch.setattr(comparison, 'generate', generate)
    cache = {}
    baseline = comparison.baseline_search('Cep Pic Med', 'Sobeys', 'secret', cache)
    assert len(order) == 2 and all(kind == 'brave' for kind, _ in order)
    assert 'Oep Pic Med' in order[1][1]
    results = [comparison.evaluate(p, 'Cep Pic Med', 'Sobeys', 'secret', .85, cache, baseline)
               for p in ('openai', 'qwen')]
    assert prompts[0] == prompts[1] and title in prompts[0]
    assert all(r['accepted'] and r['confidence'] == .9167 for r in results)
    # Two baseline hypotheses plus one identical proposed query shared by models.
    assert sum(kind == 'brave' for kind, _ in order) == 3


def test_full_expansion_finds_candidate_without_scoring_expansion_as_receipt(monkeypatch):
    title = 'Old El Paso Salsa Picante Style Restaurant Medium 650 ml'
    url = 'https://sobeys.com/products/old-el-paso-salsa-picante-style-restaurant-medium-650-ml'
    calls = []
    def search(key, query, original, domain):
        calls.append((query, original))
        if query != 'site:sobeys.com old el paso picante medium':
            return []
        return [core.SearchResult(title, url, '', core.candidate_score(original, domain, title, url))]
    monkeypatch.setattr(core, 'brave_candidates', search)
    cache = {}
    prior = {'id':7, 'merchant_key':'sobeys.com', 'status':'accepted', 'confidence':.95,
             'receipt_text_norm':'oep pic med', 'product_description':title, 'product_url':url}
    baseline = comparison.baseline_search('Cep Pic Med', 'Sobeys', 'key', cache, [prior])
    assert len(baseline) == 1 and baseline[0][1].score == .9167
    assert all(original == 'Cep Pic Med' for _, original in calls)
    assert baseline[0][0] == 'site:sobeys.com old el paso picante medium'


def test_provider_does_not_borrow_other_models_expanded_search_results(monkeypatch):
    baseline = core.SearchResult('Unknown grocery', 'https://sobeys.com/products/unknown', '', 0)
    good = core.SearchResult('Old El Paso Salsa Picante Medium',
                            'https://sobeys.com/products/salsa', '', .9167)
    def search(key, query, item, domain):
        return [good] if 'Salsa' in query else []
    monkeypatch.setattr(core, 'brave_candidates', search)
    monkeypatch.setattr(comparison, 'generate', lambda provider, text: {
        'queries': ['Salsa'] if provider == 'openai' else ['Unknown']})
    cache = {}
    left = comparison.evaluate('openai', 'Cep Pic Med', 'Sobeys', 'key', .85, cache, [('initial', baseline)])
    right = comparison.evaluate('qwen', 'Cep Pic Med', 'Sobeys', 'key', .85, cache, [('initial', baseline)])
    assert left['accepted'] and right['confidence'] == 0
