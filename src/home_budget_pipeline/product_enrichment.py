"""Enrich receipt items with retailer product descriptions and URLs."""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional

RETAILER_DOMAINS = {
    "costco": "costco.ca",
    "home depot": "homedepot.ca",
    "walmart": "walmart.ca",
    "canadian tire": "canadiantire.ca",
    "shoppers drug mart": "shoppersdrugmart.ca",
    "sobeys": "sobeys.com",
    "wholesale club": "wholesaleclub.ca",
    "old navy": "oldnavy.gapcanada.ca",
}
BARCODE_RE = re.compile(r"\b\d{8,14}\b")
RECEIPT_SUFFIX_RE = re.compile(r"(?:\s+\d+[.,]\d{2})?\s+(?:GP|LP|FP|H|A|B|T)$", re.IGNORECASE)


def retailer_domain(merchant: str) -> Optional[str]:
    normalized = re.sub(r"[^a-z0-9]+", " ", (merchant or "").lower()).strip()
    for name, domain in RETAILER_DOMAINS.items():
        if name in normalized:
            return domain
    return None


def product_query(item_name: str, merchant: str) -> Optional[str]:
    domain = retailer_domain(merchant)
    if not domain:
        return None
    barcode = BARCODE_RE.search(item_name or "")
    if barcode:
        return f'site:{domain} "{barcode.group(0)}"'
    terms = re.sub(r"<[^>]+>|[^A-Za-z0-9 .]+", " ", item_name).strip()
    terms = RECEIPT_SUFFIX_RE.sub("", terms).strip()
    terms = re.sub(r"\s+", " ", terms)
    return f"site:{domain} {terms}" if terms else None


def candidate_score(item_name: str, domain: str, title: str, url: str, snippet: str = "") -> float:
    hostname = (urllib.parse.urlparse(url).hostname or "").lower()
    if hostname != domain and not hostname.endswith(f".{domain}"):
        return 0.0
    source = f"{title} {url} {snippet}".lower()
    barcode = BARCODE_RE.search(item_name or "")
    if barcode and barcode.group(0) in source:
        return 1.0
    tokens = {t for t in re.findall(r"[a-z0-9]+", item_name.lower()) if len(t) >= 3 and not t.isdigit()}
    if not tokens:
        return 0.0
    overlap = sum(token in source for token in tokens) / len(tokens)
    return round(0.35 + 0.65 * overlap, 4)


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    score: float


def brave_search(api_key: str, query: str, item_name: str, domain: str) -> Optional[SearchResult]:
    url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode({"q": query, "count": 10, "country": "ca", "search_lang": "en"})
    request = urllib.request.Request(url, headers={"X-Subscription-Token": api_key, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.load(response)
    candidates = []
    for row in payload.get("web", {}).get("results", []):
        title, candidate_url, snippet = row.get("title", ""), row.get("url", ""), row.get("description", "")
        candidates.append(SearchResult(title, candidate_url, snippet, candidate_score(item_name, domain, title, candidate_url, snippet)))
    return max(candidates, key=lambda result: result.score, default=None)


def run(*, dsn: str, api_key: str, limit: int, threshold: float, write_db: bool) -> dict[str, int]:
    import psycopg
    from psycopg.rows import dict_row

    stats = {"considered": 0, "searched": 0, "accepted": 0, "review": 0, "unsupported": 0}
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        rows = conn.execute("""
            SELECT i.id, i.item_name, e.store_name
              FROM budget.expense_items i JOIN budget.expenses e ON e.id=i.expense_pk
             WHERE i.product_description IS NULL OR i.product_url IS NULL
               AND NOT EXISTS (
                   SELECT 1 FROM budget.product_enrichment_results r
                    WHERE r.expense_item_id=i.id
               )
               AND (e.store_name ILIKE ANY(%s))
             ORDER BY i.id LIMIT %s
        """, ([f"%{name}%" for name in RETAILER_DOMAINS], limit)).fetchall()
        cache: dict[str, Optional[SearchResult]] = {}
        for row in rows:
            stats["considered"] += 1
            query = product_query(row["item_name"], row["store_name"] or "")
            domain = retailer_domain(row["store_name"] or "")
            if not query or not domain:
                stats["unsupported"] += 1
                continue
            if query not in cache:
                previous = conn.execute("""
                    SELECT candidate_title, candidate_url, confidence
                      FROM budget.product_enrichment_results
                     WHERE provider='brave' AND search_query=%s
                       AND candidate_url IS NOT NULL
                     ORDER BY searched_at DESC LIMIT 1
                """, (query,)).fetchone()
                if previous:
                    cache[query] = SearchResult(
                        previous["candidate_title"] or "",
                        previous["candidate_url"], "", float(previous["confidence"]),
                    )
                else:
                    cache[query] = brave_search(api_key, query, row["item_name"], domain)
                    stats["searched"] += 1
            result = cache[query]
            status = "accepted" if result and result.score >= threshold else "review"
            stats[status] += 1
            if write_db:
                conn.execute("""
                    INSERT INTO budget.product_enrichment_results
                        (expense_item_id, provider, search_query, candidate_title, candidate_url, confidence, status)
                    VALUES (%s, 'brave', %s, %s, %s, %s, %s)
                    ON CONFLICT (expense_item_id) DO UPDATE SET
                        search_query=EXCLUDED.search_query, candidate_title=EXCLUDED.candidate_title,
                        candidate_url=EXCLUDED.candidate_url, confidence=EXCLUDED.confidence,
                        status=EXCLUDED.status, searched_at=NOW()
                """, (row["id"], query, result.title if result else None, result.url if result else None, result.score if result else 0, status))
                if status == "accepted":
                    conn.execute("UPDATE budget.expense_items SET product_description=%s, product_url=%s, updated_at=NOW() WHERE id=%s", (result.title, result.url, row["id"]))
                    conn.execute("""
                        INSERT INTO budget.expense_item_description_audit
                            (expense_item_id, actor_user, actor_email, old_description, new_description, old_url, new_url)
                        VALUES (%s, 'product-enrichment', NULL, NULL, %s, NULL, %s)
                    """, (row["id"], result.title, result.url))
        if write_db:
            conn.commit()
    return stats


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--threshold", type=float, default=0.85)
    parser.add_argument("--write-db", action="store_true")
    args = parser.parse_args()
    dsn = os.environ.get("DATABASE_URL", "")
    api_key = os.environ.get("BRAVE_SEARCH_API_KEY", "")
    if not dsn or not api_key:
        raise RuntimeError("DATABASE_URL and BRAVE_SEARCH_API_KEY are required")
    print(json.dumps(run(dsn=dsn, api_key=api_key, limit=args.limit, threshold=args.threshold, write_db=args.write_db), sort_keys=True))
    return 0
