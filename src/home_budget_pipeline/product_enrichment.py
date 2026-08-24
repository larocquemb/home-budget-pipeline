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
NOISE_TOKENS = {"acct", "cad", "each", "flash", "gp", "lp", "save", "upto"}
PRODUCT_PATH_MARKERS = {
    "costco.ca": (".product.", "/product/"),
    "homedepot.ca": ("/product/",),
    "walmart.ca": ("/ip/",),
    "canadiantire.ca": ("/pdp/",),
    "shoppersdrugmart.ca": ("/p/",),
    "sobeys.com": ("/products/",),
    "wholesaleclub.ca": ("/product/",),
    "oldnavy.gapcanada.ca": ("/browse/product.do",),
}


def retailer_domain(merchant: str) -> Optional[str]:
    normalized = re.sub(r"[^a-z0-9]+", " ", (merchant or "").lower()).strip()
    for name, domain in RETAILER_DOMAINS.items():
        if name in normalized:
            return domain
    return None


def normalized_cache_item_name(item_name: str) -> str:
    """Stable key for retailer receipt text before enrichment mutates display fields."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (item_name or "").lower())).strip()


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
    tokens = {
        t for t in re.findall(r"[a-z0-9]+", item_name.lower())
        if len(t) >= 3 and not t.isdigit() and t not in NOISE_TOKENS
    }
    if len(tokens) < 2:
        return 0.0
    overlap = sum(token in source for token in tokens) / len(tokens)
    score = 0.35 + 0.65 * overlap
    path = urllib.parse.urlparse(url).path.lower()
    if not any(marker in path for marker in PRODUCT_PATH_MARKERS.get(domain, ())):
        score = min(score, 0.8)
    return round(score, 4)


def normalized_item_name_from_verified_product(item_name: str, product_title: str, confidence: float) -> Optional[str]:
    """Correct a one-glyph brand initialism using strongly verified product evidence."""
    if confidence < 0.95:
        return None
    item_tokens = item_name.split()
    title_tokens = re.findall(r"[A-Za-z0-9]+", product_title)
    if not item_tokens or not 2 <= len(item_tokens[0]) <= 5 or len(title_tokens) < len(item_tokens[0]):
        return None
    observed = item_tokens[0].lower()
    initialism = "".join(token[0] for token in title_tokens[: len(observed)]).lower()
    if sum(left != right for left, right in zip(observed, initialism)) != 1:
        return None
    return " ".join([initialism.capitalize(), *item_tokens[1:]])


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    score: float


def ai_product_queries(item_name: str, merchant: str, *, client: object | None = None) -> tuple[str, ...]:
    """Ask AI for search expansions; returned text is never product evidence."""
    if os.getenv("ENABLE_AI_PRODUCT_FALLBACK", "1").lower() not in {"1", "true", "yes"}:
        return ()
    if client is None:
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            return ()
        try:
            from openai import OpenAI
        except ImportError:
            return ()
        client = OpenAI(api_key=api_key, timeout=float(os.getenv("AI_TIMEOUT_SECONDS", "12")), max_retries=1)
    schema = {
        "type": "object", "additionalProperties": False, "required": ["queries"],
        "properties": {"queries": {"type": "array", "maxItems": 3, "items": {"type": "string"}}},
    }
    spelling_variants = [item_name]
    first_token = item_name.split(maxsplit=1)
    if first_token and first_token[0][:1].lower() in {"c", "o"}:
        alternate_initial = "O" if first_token[0][0].lower() == "c" else "C"
        alternate_token = alternate_initial + first_token[0][1:]
        spelling_variants.append(" ".join([alternate_token, *first_token[1:]]))
    prompt = (
        "Expand an abbreviated or OCR-damaged retail receipt item into up to three concise "
        "product search queries. Do not claim that any expansion is correct; search results "
        "will verify it. Account for every significant receipt token, consider every supplied "
        "OCR spelling, and order the most plausible grocery-retailer interpretation first. "
        "Treat a short token as a possible initialism for a multiword grocery brand. Every short "
        "receipt token must be replaced by plausible full words: do not return CEP, OEP, PIC, or "
        "MED unchanged. Do not silently drop an unexplained token. Prefer grocery products over "
        "medicine unless the other receipt tokens support medicine.\n"
        f"Merchant: {merchant}\nPossible OCR spellings: {'; '.join(spelling_variants)}"
    )
    forbidden = {
        token.lower() for variant in spelling_variants for token in re.findall(r"[A-Za-z0-9]+", variant)
        if len(token) <= 4
    }
    for attempt in range(2):
        try:
            response = client.responses.create(
                model=os.getenv("AI_PRODUCT_MODEL", "gpt-5.6-terra"), input=prompt,
                reasoning={"effort": "low"}, max_output_tokens=400,
                text={"format": {"type": "json_schema", "name": "product_search_queries", "strict": True, "schema": schema}},
            )
            payload = json.loads((response.output_text or "").strip())
            queries = tuple(dict.fromkeys(
                query.strip() for query in payload.get("queries", ())
                if isinstance(query, str) and query.strip()
                and not (forbidden & set(re.findall(r"[a-z0-9]+", query.lower())))
            ))[:3]
            if queries or attempt:
                return queries
            prompt += "\nYour previous proposals retained receipt abbreviations. Try again and fully expand the likely brand and product words; return an empty list if you cannot."
        except Exception:
            return ()
    return ()


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


def run(*, dsn: str, api_key: str, limit: int, threshold: float, write_db: bool, item_ids: tuple[int, ...] = ()) -> dict[str, int]:
    import psycopg
    from psycopg.rows import dict_row

    stats = {"considered": 0, "db_hits": 0, "searched": 0, "ai_queries": 0, "ai_expanded": 0, "accepted": 0, "review": 0, "unsupported": 0}
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        result_filter = "" if item_ids else """AND NOT EXISTS (
                   SELECT 1 FROM budget.product_enrichment_results r WHERE r.expense_item_id=i.id
               )"""
        item_filter = "AND i.id = ANY(%s)" if item_ids else ""
        missing_filter = "TRUE" if item_ids else "(i.product_description IS NULL OR i.product_url IS NULL)"
        params = ([f"%{name}%" for name in RETAILER_DOMAINS],)
        if item_ids:
            params += (list(item_ids),)
        params += (limit,)
        rows = conn.execute(f"""
            SELECT i.id, i.item_name, e.store_name
              FROM budget.expense_items i JOIN budget.expenses e ON e.id=i.expense_pk
             WHERE {missing_filter} {result_filter}
               AND (e.store_name ILIKE ANY(%s)) {item_filter}
             ORDER BY i.id LIMIT %s
        """, params).fetchall()
        cache: dict[str, Optional[SearchResult]] = {}
        expansion_cache: dict[tuple[str, str], tuple[str, ...]] = {}
        for row in rows:
            stats["considered"] += 1
            original_item_name = row["item_name"]
            merchant = row["store_name"] or ""
            domain = retailer_domain(merchant)
            query = product_query(original_item_name, merchant)
            if not query or not domain:
                stats["unsupported"] += 1
                continue

            item_key = normalized_cache_item_name(original_item_name)
            previous = conn.execute("""
                SELECT provider, search_query, product_description, product_url, confidence
                  FROM enrichment.product_cache
                 WHERE merchant_key=%s AND receipt_text_norm=%s
                   AND status='accepted' AND confidence >= %s
                 ORDER BY confidence DESC, last_used_at DESC LIMIT 1
            """, (domain, item_key, threshold)).fetchone()

            if previous:
                result = SearchResult(previous["product_description"], previous["product_url"], "", float(previous["confidence"]))
                selected_query = previous["search_query"] or query
                provider = "db-cache"
                stats["db_hits"] += 1
            else:
                if query not in cache:
                    old_search = conn.execute("""
                        SELECT candidate_title, candidate_url, confidence
                          FROM budget.product_enrichment_results
                         WHERE provider='brave' AND search_query=%s AND candidate_url IS NOT NULL
                         ORDER BY searched_at DESC LIMIT 1
                    """, (query,)).fetchone()
                    if old_search:
                        cache[query] = SearchResult(old_search["candidate_title"] or "", old_search["candidate_url"], "", float(old_search["confidence"]))
                    else:
                        cache[query] = brave_search(api_key, query, original_item_name, domain)
                        stats["searched"] += 1
                result = cache[query]
                selected_query = query
                provider = "brave"
                if not result or result.score < threshold:
                    expansion_key = (original_item_name, merchant)
                    if expansion_key not in expansion_cache:
                        expansion_cache[expansion_key] = ai_product_queries(*expansion_key)
                        stats["ai_queries"] += len(expansion_cache[expansion_key])
                    for expansion in expansion_cache[expansion_key]:
                        expanded_query = f"site:{domain} {expansion}"
                        if expanded_query not in cache:
                            cache[expanded_query] = brave_search(api_key, expanded_query, expansion, domain)
                            stats["searched"] += 1
                        expanded_result = cache[expanded_query]
                        if expanded_result and (not result or expanded_result.score > result.score):
                            result = expanded_result
                            selected_query = expanded_query
                            provider = "brave+ai"
                    if provider == "brave+ai":
                        stats["ai_expanded"] += 1

            status = "accepted" if result and result.score >= threshold else "review"
            stats[status] += 1
            if write_db:
                conn.execute("""
                    INSERT INTO budget.product_enrichment_results
                        (expense_item_id, provider, search_query, candidate_title, candidate_url, confidence, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (expense_item_id) DO UPDATE SET
                        provider=EXCLUDED.provider, search_query=EXCLUDED.search_query,
                        candidate_title=EXCLUDED.candidate_title, candidate_url=EXCLUDED.candidate_url,
                        confidence=EXCLUDED.confidence, status=EXCLUDED.status, searched_at=NOW()
                """, (row["id"], provider, selected_query, result.title if result else None, result.url if result else None, result.score if result else 0, status))
                if status == "accepted":
                    if provider == "db-cache":
                        conn.execute("""
                            UPDATE enrichment.product_cache
                               SET last_used_at=NOW(), use_count=use_count + 1
                             WHERE merchant_key=%s AND receipt_text_norm=%s
                        """, (domain, item_key))
                    else:
                        conn.execute("""
                            INSERT INTO enrichment.product_cache
                                (merchant_key, receipt_text_norm, product_description, product_url,
                                 provider, search_query, confidence, status)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, 'accepted')
                            ON CONFLICT (merchant_key, receipt_text_norm) DO UPDATE SET
                                product_description=EXCLUDED.product_description,
                                product_url=EXCLUDED.product_url,
                                provider=EXCLUDED.provider,
                                search_query=EXCLUDED.search_query,
                                confidence=EXCLUDED.confidence,
                                status='accepted',
                                last_used_at=NOW(),
                                use_count=enrichment.product_cache.use_count + 1
                        """, (domain, item_key, result.title, result.url, provider, selected_query, result.score))
                    normalized_name = normalized_item_name_from_verified_product(original_item_name, result.title, result.score)
                    conn.execute("""UPDATE budget.expense_items
                                      SET item_name=COALESCE(%s, item_name), product_description=%s,
                                          product_url=%s, updated_at=NOW() WHERE id=%s""",
                                 (normalized_name, result.title, result.url, row["id"]))
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
    parser.add_argument("--item-id", type=int, action="append", default=[])
    parser.add_argument("--write-db", action="store_true")
    args = parser.parse_args()
    dsn = os.environ.get("DATABASE_URL", "")
    api_key = os.environ.get("BRAVE_SEARCH_API_KEY", "")
    if not dsn or not api_key:
        raise RuntimeError("DATABASE_URL and BRAVE_SEARCH_API_KEY are required")
    print(json.dumps(run(dsn=dsn, api_key=api_key, limit=args.limit, threshold=args.threshold, write_db=args.write_db, item_ids=tuple(args.item_id)), sort_keys=True))
    return 0
