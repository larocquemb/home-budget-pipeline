"""Enrich receipt items with retailer product descriptions and URLs."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional

LOG = logging.getLogger(__name__)

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


def accepted_search_priors(conn, merchant: str) -> list[dict]:
    """Retrieve bounded retailer-specific accepted matches, never experiments."""
    domain = retailer_domain(merchant)
    if not domain:
        return []
    return conn.execute("""SELECT id,merchant_key,receipt_text_norm,product_description,
        product_url,status,confidence FROM enrichment.product_cache
        WHERE merchant_key=%s AND status='accepted' AND confidence >= 0.9
        ORDER BY last_used_at DESC,id DESC LIMIT 1000""", (domain,)).fetchall()


def learned_discovery(item_name: str, merchant: str, prior_matches=()) -> list[dict]:
    """Learn search hypotheses from accepted pairs; preserve auditable sources."""
    domain = retailer_domain(merchant)
    if not domain or BARCODE_RE.search(item_name or ''):
        return []
    rules = {}
    for prior in prior_matches:
        if prior.get('status') != 'accepted' or prior.get('merchant_key') != domain:
            continue
        if float(prior.get('confidence') or 0) < .9:
            continue
        original = prior.get('receipt_text_norm') or ''
        title, url = prior.get('product_description') or '', prior.get('product_url') or ''
        if candidate_score(original, domain, title, url) < .9:
            continue
        tokens = re.findall(r'[a-z0-9]+', original.lower())
        words = re.findall(r'[a-z0-9]+', title.lower())
        while words and words[0] in {'buy', 'shop', 'purchase'}:
            words.pop(0)
        source = {'cache_id': prior['id'], 'receipt_text': original,
                  'product_title': title, 'product_url': url}
        for index, token in enumerate(tokens):
            expansion = None
            if index == 0 and 2 <= len(token) <= 5 and len(words) >= len(token):
                initials = ''.join(w[0] for w in words[:len(token)])
                differences = [(a,b) for a,b in zip(token, initials) if a != b]
                if not differences or differences in [[('c','o')], [('o','c')]]:
                    expansion = ' '.join(words[:len(token)])
            if not expansion and len(token) >= 3:
                matches = {w for w in words if w.startswith(token) and w != token}
                if len(matches) == 1:
                    expansion = matches.pop()
            if expansion:
                rules.setdefault(token, {}).setdefault(expansion, {})[prior['id']] = source
    tokens = re.findall(r'[a-z0-9]+', item_name.lower())
    options = []
    for index, token in enumerate(tokens):
        choices = dict(rules.get(token, {}))
        if index == 0 and 2 <= len(token) <= 5 and token[0] in {'c', 'o'}:
            alternate = ('o' if token[0] == 'c' else 'c') + token[1:]
            for expansion, sources in rules.get(alternate, {}).items():
                choices.setdefault(expansion, {}).update(sources)
        options.append(sorted(choices.items(), key=lambda p: (-len(p[1]), p[0]))[:2] or [(token, {})])
    combinations = [([], {})]
    for choices in options:
        combinations = [(terms+[word], {**sources, **support})
                        for terms,sources in combinations for word,support in choices][:4]
    return [{'query': product_query(' '.join(terms), merchant),
             'prior_matches': list(sources.values())}
            for terms,sources in combinations if sources]


def discovery_queries(item_name: str, merchant: str, prior_matches=()) -> tuple[str, ...]:
    """Original search plus OCR alternatives and expansions learned from history."""
    query = product_query(item_name, merchant)
    if BARCODE_RE.search(item_name or ''):
        return (query,) if query else ()
    tokens = item_name.split()
    queries = [query]
    if tokens and 2 <= len(tokens[0]) <= 5 and tokens[0][0].lower() in {'c', 'o'}:
        corrected = ('O' if tokens[0][0].lower() == 'c' else 'C') + tokens[0][1:]
        queries.append(product_query(' '.join([corrected, *tokens[1:]]), merchant))
    queries.extend(entry['query'] for entry in learned_discovery(item_name, merchant, prior_matches))
    return tuple(dict.fromkeys(q for q in queries if q))[:6]


def scoped_search_query(expansion: str, merchant: str) -> Optional[str]:
    """Apply the retailer scope once, including model-supplied site queries."""
    domain = retailer_domain(merchant)
    terms = re.sub(r'\bsite:\S+', '', expansion, flags=re.IGNORECASE).strip()
    return f'site:{domain} {terms}' if domain and terms else None


def candidate_evidence(item_name: str, domain: str, title: str, url: str, snippet: str = "") -> dict:
    """Explain lexical evidence; proposals themselves never increase this score."""
    hostname = (urllib.parse.urlparse(url).hostname or "").lower()
    if hostname != domain and not hostname.endswith(f".{domain}"):
        return {"confidence": 0.0, "product_page": False, "reason": "wrong_retailer", "tokens": []}
    path = urllib.parse.urlparse(url).path.lower()
    product_page = any(marker in path for marker in PRODUCT_PATH_MARKERS.get(domain, ()))
    if re.search(r"/(brand|brands|category|categories|recipes)(/|$)", path):
        product_page = False
    source_words = set(re.findall(r"[a-z0-9]+", f"{title} {urllib.parse.unquote(path)} {snippet}".lower()))
    barcode = BARCODE_RE.search(item_name or "")
    if barcode and barcode.group(0) in source_words:
        return {"confidence": 1.0 if product_page else 0.0, "product_page": product_page,
                "reason": "exact_barcode", "tokens": []}
    tokens = list(dict.fromkeys(
        t for t in re.findall(r"[a-z0-9]+", item_name.lower())
        if len(t) >= 3 and not t.isdigit() and t not in NOISE_TOKENS
    ))
    title_words = re.findall(r"[a-z0-9]+", title.lower())
    while title_words and title_words[0] in {"buy", "shop", "purchase"}:
        title_words.pop(0)
    evidence = []
    for index, token in enumerate(tokens):
        weight, kind, matched = 0.0, "unmatched", None
        if token in source_words:
            weight, kind, matched = 1.0, "exact_token", token
        elif index == 0 and 2 <= len(token) <= 5 and len(title_words) >= len(token):
            initialism = "".join(word[0] for word in title_words[:len(token)])
            differences = [(a, b) for a, b in zip(token, initialism) if a != b]
            if not differences:
                weight, kind, matched = 1.0, "brand_initialism", initialism
            elif differences == [("c", "o")] or differences == [("o", "c")]:
                weight, kind, matched = .95, "ocr_brand_initialism", initialism
        if weight == 0:
            prefixes = sorted(word for word in source_words if word.startswith(token) and word != token)
            if prefixes:
                weight, kind, matched = .9, "token_prefix", prefixes[0]
        if weight == 0:
            synonyms = {"sauce": {"salsa"}, "salsa": {"sauce"}}.get(token, set()) & source_words
            if synonyms:
                weight, kind, matched = .85, "category_synonym", sorted(synonyms)[0]
        evidence.append({"token": token, "kind": kind, "matched": matched, "weight": weight})
    # At least two meaningful tokens are needed without an exact barcode.
    score = sum(e["weight"] for e in evidence) / len(evidence) if len(evidence) >= 2 else 0.0
    if any(e["kind"] == "unmatched" for e in evidence):
        score = min(score, .84)
    if not product_page:
        score = 0.0
    return {"confidence": round(score, 4), "product_page": product_page,
            "reason": "receipt_token_evidence" if product_page else "non_product_page", "tokens": evidence}


def candidate_score(item_name: str, domain: str, title: str, url: str, snippet: str = "") -> float:
    return candidate_evidence(item_name, domain, title, url, snippet)["confidence"]


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
    return max(brave_candidates(api_key, query, item_name, domain), key=lambda result: result.score, default=None)


def brave_query_results(api_key: str, query: str, *, timeout: float | None = None) -> list[dict]:
    from .brave_query_cache import ACTIVE_CACHE
    cache = ACTIVE_CACHE.get()
    if cache is not None:
        return cache.get(query, lambda: _fetch_brave_query(api_key, query, timeout=timeout))
    return _fetch_brave_query(api_key, query, timeout=timeout)


def _fetch_brave_query(api_key: str, query: str, *, timeout: float | None = None) -> list[dict]:
    url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode({"q": query, "count": 10, "country": "ca", "search_lang": "en"})
    request = urllib.request.Request(url, headers={"X-Subscription-Token": api_key, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout if timeout is not None else float(os.getenv("BRAVE_TIMEOUT_SECONDS", "20"))) as response:
        payload = json.load(response)
    if not isinstance(payload, dict) or payload.get('error') or payload.get('type') == 'ErrorResponse':
        raise ValueError('Brave returned an error response')
    rows = payload.get('web', {}).get('results', [])
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError('Invalid Brave search results')
    return [{key: str(row.get(field) or '') for key, field in
             [('title', 'title'), ('url', 'url'), ('snippet', 'description')]} for row in rows]


def brave_candidates(api_key: str, query: str, item_name: str, domain: str) -> list[SearchResult]:
    candidates = []
    for row in brave_query_results(api_key, query):
        title, candidate_url, snippet = row['title'], row['url'], row['snippet']
        candidates.append(SearchResult(title, candidate_url, snippet, candidate_score(item_name, domain, title, candidate_url, snippet)))
    return candidates


def run(*, dsn: str, api_key: str, limit: int, threshold: float, write_db: bool, item_ids: tuple[int, ...] = ()) -> dict[str, int]:
    import psycopg
    from psycopg.rows import dict_row

    stats = {"considered": 0, "db_hits": 0, "searched": 0, "ai_queries": 0, "ai_expanded": 0, "accepted": 0, "review": 0, "unsupported": 0}
    from .brave_query_cache import BraveQueryCache, refresh_requested
    with psycopg.connect(dsn, row_factory=dict_row) as conn, BraveQueryCache(dsn, refresh=refresh_requested()) as query_cache:
        refresh = refresh_requested()
        result_filter = "" if item_ids or refresh else """AND NOT EXISTS (
                   SELECT 1 FROM budget.product_enrichment_results r WHERE r.expense_item_id=i.id
               )"""
        item_filter = "AND i.id = ANY(%s)" if item_ids else ""
        missing_filter = "TRUE" if item_ids or refresh else "(i.product_description IS NULL OR i.product_url IS NULL)"
        params = ([f"%{name}%" for name in RETAILER_DOMAINS],)
        if item_ids:
            params += (list(item_ids),)
        params += (refresh, limit)
        rows = conn.execute(f"""
            SELECT i.id, i.item_name, e.store_name, i.expense_pk AS receipt_id
              FROM budget.expense_items i JOIN budget.expenses e ON e.id=i.expense_pk
             WHERE {missing_filter} {result_filter}
               AND (e.store_name ILIKE ANY(%s)) {item_filter}
               AND (%s OR NOT (COALESCE(i.product_description,'')<>'' AND COALESCE(i.product_url,'')<>''
                   AND EXISTS (SELECT 1 FROM budget.product_enrichment_results accepted
                       WHERE accepted.expense_item_id=i.id AND accepted.status='accepted'
                         AND accepted.ocr_evidence ? 'readings')))
             ORDER BY i.id LIMIT %s
        """, params).fetchall()
        cache: dict[tuple[str, str], Optional[SearchResult]] = {}
        expansion_cache: dict[tuple[str, str], tuple[str, ...]] = {}
        receipt_readings = {}
        from .receipt_ocr_alternatives import load as load_ocr_alternatives
        LOG.info("enrichment_selected items=%s write_db=%s", len(rows), write_db)
        for index, row in enumerate(rows, start=1):
            LOG.info("enrichment_item_started item=%s/%s item_id=%s merchant=%s description=%r",
                     index, len(rows), row['id'], row['store_name'], row['item_name'])
            query_cache.context = {'item_id': row['id'], 'merchant': row['store_name']}
            stats["considered"] += 1
            original_item_name = row["item_name"]
            merchant = row["store_name"] or ""
            domain = retailer_domain(merchant)
            query = product_query(original_item_name, merchant)
            if not query or not domain:
                stats["unsupported"] += 1
                LOG.info("enrichment_item_completed item_id=%s status=unsupported", row['id'])
                continue

            readings = [{'text': original_item_name, 'sources': []}]
            artifact_errors = []
            if row.get('receipt_id') is not None:
                if row['receipt_id'] not in receipt_readings:
                    receipt_readings[row['receipt_id']] = load_ocr_alternatives(conn, row['receipt_id'])
                by_item, artifact_errors = receipt_readings[row['receipt_id']]
                readings = by_item.get(row['id'], readings)
            selected_reading = original_item_name
            evaluated_candidates = []
            LOG.info("enrichment_ocr_readings item_id=%s alternatives=%s artifact_errors=%s",
                     row['id'], len(readings), artifact_errors)
            item_key = normalized_cache_item_name(original_item_name)
            previous = conn.execute("""
                SELECT provider, search_query, product_description, product_url, confidence
                  FROM enrichment.product_cache
                 WHERE merchant_key=%s AND receipt_text_norm=%s
                   AND status='accepted' AND confidence >= %s
                 ORDER BY confidence DESC, last_used_at DESC LIMIT 1
            """, (domain, item_key, threshold)).fetchone()

            previous_score = candidate_score(original_item_name, domain, previous["product_description"] or "", previous["product_url"] or "") if previous else 0
            if previous and previous_score >= threshold and not refresh and len(readings) == 1:
                result = SearchResult(previous["product_description"], previous["product_url"], "", previous_score)
                selected_query = previous["search_query"] or query
                provider = "db-cache"
                stats["db_hits"] += 1
                evaluated_candidates.append({'reading': original_item_name, 'title': result.title,
                                             'url': result.url, 'confidence': result.score})
            else:
                result = None
                selected_query = query
                provider = "brave"
                # Try every aligned OCR spelling before asking AI to expand it.
                for reading in readings:
                    text = reading['text']
                    reading_query = product_query(text, merchant)
                    if not reading_query:
                        continue
                    query_key = (text, reading_query)
                    if query_key not in cache:
                        LOG.info("enrichment_search_started item_id=%s provider=brave reading=%r",
                                 row["id"], text)
                        cache[query_key] = brave_search(api_key, reading_query, text, domain)
                        stats["searched"] += 1
                    candidate = cache[query_key]
                    if candidate:
                        evaluated_candidates.append({'reading': text, 'title': candidate.title,
                                                     'url': candidate.url, 'confidence': candidate.score})
                    if candidate and (not result or candidate.score > result.score):
                        result, selected_query, selected_reading = candidate, reading_query, text
                if not result or result.score < threshold:
                    for reading in readings:
                        text = reading['text']
                        expansion_key = (text, merchant)
                        if expansion_key not in expansion_cache:
                            LOG.info("enrichment_ai_started item_id=%s reading=%r", row["id"], text)
                            expansion_cache[expansion_key] = ai_product_queries(*expansion_key)
                            stats["ai_queries"] += len(expansion_cache[expansion_key])
                        for expansion in expansion_cache[expansion_key]:
                            expanded_query = scoped_search_query(expansion, merchant)
                            if not expanded_query:
                                continue
                            expanded_key = (text, expanded_query)
                            if expanded_key not in cache:
                                cache[expanded_key] = brave_search(api_key, expanded_query, text, domain)
                                stats["searched"] += 1
                            expanded_result = cache[expanded_key]
                            if expanded_result:
                                evaluated_candidates.append({'reading': text, 'title': expanded_result.title,
                                                             'url': expanded_result.url,
                                                             'confidence': expanded_result.score})
                            if expanded_result and (not result or expanded_result.score > result.score):
                                result = expanded_result
                                selected_query = expanded_query
                                selected_reading = text
                                provider = "brave+ai"
                    if provider == "brave+ai":
                        stats["ai_expanded"] += 1

            status = "accepted" if result and result.score >= threshold else "review"
            conflicting_products = bool(result and any(
                candidate['reading'] != selected_reading and candidate['url'] != result.url
                and candidate['confidence'] >= threshold
                and result.score - candidate['confidence'] <= .02
                for candidate in evaluated_candidates))
            if conflicting_products:
                status = "review"
                LOG.info("enrichment_ocr_conflict item_id=%s status=review", row['id'])
            stats[status] += 1
            if write_db:
                conn.execute("""
                    INSERT INTO budget.product_enrichment_results
                        (expense_item_id, provider, search_query, candidate_title, candidate_url, confidence, status, ocr_evidence)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (expense_item_id) DO UPDATE SET
                        provider=EXCLUDED.provider, search_query=EXCLUDED.search_query,
                        candidate_title=EXCLUDED.candidate_title, candidate_url=EXCLUDED.candidate_url,
                        confidence=EXCLUDED.confidence, status=EXCLUDED.status,
                        ocr_evidence=EXCLUDED.ocr_evidence, searched_at=NOW()
                """, (row["id"], provider, selected_query, result.title if result else None, result.url if result else None, result.score if result else 0, status,
                    json.dumps({'selected_reading': selected_reading, 'readings': readings,
                                'artifact_errors': artifact_errors,
                                'candidates': evaluated_candidates,
                                'conflicting_products': conflicting_products})))
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
                    normalized_name = normalized_item_name_from_verified_product(selected_reading, result.title, result.score)
                    conn.execute("""UPDATE budget.expense_items
                                      SET item_name=COALESCE(%s, item_name), product_description=%s,
                                          product_url=%s, updated_at=NOW() WHERE id=%s""",
                                 (normalized_name or (selected_reading if selected_reading != original_item_name else None),
                                  result.title, result.url, row["id"]))
                    conn.execute("""
                        INSERT INTO budget.expense_item_description_audit
                            (expense_item_id, actor_user, actor_email, old_description, new_description, old_url, new_url)
                        VALUES (%s, 'product-enrichment', NULL, NULL, %s, NULL, %s)
                    """, (row["id"], result.title, result.url))
            LOG.info("enrichment_item_completed item_id=%s status=%s provider=%s confidence=%.4f saved=%s reading=%r",
                     row['id'], status, provider, result.score if result else 0, write_db, selected_reading)
        if write_db:
            conn.commit()
            LOG.info("enrichment_committed items=%s", stats['considered'])
        stats['brave_api_requests'] = query_cache.requests
        stats['brave_cache_hits'] = query_cache.hits
    return stats


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--threshold", type=float, default=0.85)
    parser.add_argument("--item-id", type=int, action="append", default=[])
    parser.add_argument("--write-db", action="store_true")
    parser.add_argument("--compare-models", action="store_true",
                        help="Run OpenAI and Qwen on the same items; store comparison evidence only")
    parser.add_argument("--collaborate-models", action="store_true",
                        help="Share OCR and retailer evidence across models, then reconcile recommendations")
    parser.add_argument("--receipt", help="Restrict enrichment to a receipt filename or database id")
    args = parser.parse_args()
    if args.compare_models and args.collaborate_models:
        parser.error("Choose comparison or collaboration mode")
    dsn = os.environ.get("DATABASE_URL", "")
    api_key = os.environ.get("BRAVE_SEARCH_API_KEY", "")
    if not dsn or not api_key:
        raise RuntimeError("DATABASE_URL and BRAVE_SEARCH_API_KEY are required")
    item_ids = tuple(args.item_id)
    if args.receipt:
        from .receipt_enrichment import resolve_item_ids
        item_ids = resolve_item_ids(dsn, args.receipt)
        if not item_ids:
            raise RuntimeError(f"No line items found for receipt {args.receipt!r}")
    runner = run
    if args.compare_models:
        from .product_comparison import run as runner
    elif args.collaborate_models:
        from .receipt_collaboration import run as runner
    result = runner(dsn=dsn, api_key=api_key, limit=args.limit, threshold=args.threshold,
                    write_db=args.write_db, item_ids=item_ids)
    print(json.dumps(result, sort_keys=True))
    return 1 if result.get("incomplete", 0) else 0
