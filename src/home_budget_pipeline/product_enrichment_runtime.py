"""Runtime wrapper for bounded, observable product enrichment external calls."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from typing import Optional

from home_budget_pipeline import product_enrichment as core

_core_ai_product_queries = core.ai_product_queries


def _log(message: str) -> None:
    print(message, flush=True)


def brave_search(
    api_key: str,
    query: str,
    item_name: str,
    domain: str,
) -> Optional[core.SearchResult]:
    """Run a Brave lookup with a bounded timeout and immediate timing logs."""
    timeout = float(os.getenv("BRAVE_TIMEOUT_SECONDS", "10"))
    started = time.monotonic()
    _log(f"enrichment brave_start timeout={timeout:g}s query={query!r}")
    try:
        rows = core.brave_query_results(api_key, query, timeout=timeout)
    except Exception as exc:
        elapsed = time.monotonic() - started
        _log(
            f"enrichment brave_error seconds={elapsed:.2f} "
            f"type={type(exc).__name__} query={query!r}"
        )
        return None

    candidates = []
    for row in rows:
        title = row.get("title", "")
        candidate_url = row.get("url", "")
        snippet = row.get("snippet", "")
        candidates.append(
            core.SearchResult(
                title,
                candidate_url,
                snippet,
                core.candidate_score(item_name, domain, title, candidate_url, snippet),
            )
        )
    result = max(candidates, key=lambda candidate: candidate.score, default=None)
    elapsed = time.monotonic() - started
    _log(
        f"enrichment brave_done seconds={elapsed:.2f} "
        f"score={result.score if result else 0:.4f} query={query!r}"
    )
    return result


def ai_product_queries(item_name: str, merchant: str) -> tuple[str, ...]:
    """Run AI fallback once with a bounded timeout and at most two expansions."""
    if os.getenv("ENABLE_AI_PRODUCT_FALLBACK", "1").lower() not in {"1", "true", "yes"}:
        return ()
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return ()

    timeout = float(os.getenv("AI_TIMEOUT_SECONDS", "8"))
    max_queries = max(0, int(os.getenv("AI_PRODUCT_MAX_QUERIES", "2")))
    started = time.monotonic()
    _log(
        f"enrichment ai_start timeout={timeout:g}s max_queries={max_queries} "
        f"item={item_name!r} merchant={merchant!r}"
    )
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key, timeout=timeout, max_retries=0)
        queries = _core_ai_product_queries(item_name, merchant, client=client)[:max_queries]
    except Exception as exc:
        elapsed = time.monotonic() - started
        _log(
            f"enrichment ai_error seconds={elapsed:.2f} "
            f"type={type(exc).__name__} item={item_name!r}"
        )
        return ()

    elapsed = time.monotonic() - started
    _log(
        f"enrichment ai_done seconds={elapsed:.2f} queries={len(queries)} "
        f"item={item_name!r}"
    )
    return queries


def main() -> int:
    if "--compare-models" in sys.argv or "--collaborate-models" in sys.argv:
        # Comparison must distinguish search failures from legitimate no matches.
        return core.main()
    core.brave_search = brave_search
    core.ai_product_queries = ai_product_queries
    _log(
        "enrichment runtime "
        f"brave_timeout={os.getenv('BRAVE_TIMEOUT_SECONDS', '10')}s "
        f"ai_timeout={os.getenv('AI_TIMEOUT_SECONDS', '8')}s "
        f"ai_max_queries={os.getenv('AI_PRODUCT_MAX_QUERIES', '2')}"
    )
    return core.main()
