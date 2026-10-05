import inspect
import json
from types import SimpleNamespace

from home_budget_pipeline import product_enrichment
from home_budget_pipeline.product_enrichment import (
    ai_product_queries,
    candidate_score,
    normalized_item_name_from_verified_product,
    product_query,
    retailer_domain,
)


def test_discovery_searches_correction_abbreviations_and_full_brand():
    queries = product_enrichment.discovery_queries('Cep Pic Med', 'Sobeys')
    assert queries == (
        'site:sobeys.com Cep Pic Med', 'site:sobeys.com Oep Pic Med',
        'site:sobeys.com Cep Picante Medium', 'site:sobeys.com Oep Picante Medium',
        'site:sobeys.com Old El Paso Picante Medium',
    )
    assert product_enrichment.discovery_queries('058300854014', 'Sobeys') == (
        'site:sobeys.com "058300854014"',)
    assert product_enrichment.discovery_queries('Carrots', 'Sobeys') == ('site:sobeys.com Carrots',)
    assert not product_enrichment.discovery_queries('Cep Pic Med', 'Unknown merchant')


def test_model_search_scope_is_not_duplicated_or_redirected():
    query = product_enrichment.scoped_search_query('site:other.com site:sobeys.com/products Old El Paso Medium', 'Sobeys')
    assert query == 'site:sobeys.com Old El Paso Medium'


def test_pending_query_does_not_reprocess_existing_review_results():
    source = inspect.getsource(product_enrichment.run)
    assert 'missing_filter = "TRUE" if item_ids else' in source
    assert 'result_filter = "" if item_ids else' in source


def test_ai_product_queries_returns_bounded_structured_expansions():
    response = SimpleNamespace(
        output_text=json.dumps(
            {
                "queries": [
                    "Old El Paso Salsa Picante Medium",
                    "Old El Paso Salsa Picante Medium",
                    "Old El Paso Medium Salsa",
                ]
            }
        )
    )
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return response

    client = SimpleNamespace(responses=SimpleNamespace(create=create))

    assert ai_product_queries("Cep Pic Med", "Sobeys", client=client) == (
        "Old El Paso Salsa Picante Medium",
        "Old El Paso Medium Salsa",
    )
    assert "Cep Pic Med; Oep Pic Med" in calls[0]["input"]


def test_ai_product_queries_retries_unexpanded_receipt_tokens():
    responses = iter(
        [
            SimpleNamespace(output_text=json.dumps({"queries": ["Cepacol Pic Med", "Oep Pic Med"]})),
            SimpleNamespace(output_text=json.dumps({"queries": ["Old El Paso Salsa Picante Medium"]})),
        ]
    )
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return next(responses)

    client = SimpleNamespace(responses=SimpleNamespace(create=create))

    assert ai_product_queries("Cep Pic Med", "Sobeys", client=client) == (
        "Old El Paso Salsa Picante Medium",
    )
    assert len(calls) == 2
    assert "previous proposals retained receipt abbreviations" in calls[1]["input"]


def test_retailer_query_prefers_barcode_and_domain():
    assert retailer_domain("The Home Depot") == "homedepot.ca"
    assert product_query("079594233699 5PK YARD BAG <A>", "The Home Depot") == 'site:homedepot.ca "079594233699"'


def test_retailer_query_removes_receipt_price_and_tax_suffix_without_exact_phrase():
    assert product_query("DOVE CLINL ~R0 9.99 GP", "Shoppers Drug Mart") == "site:shoppersdrugmart.ca DOVE CLINL R0"


def test_candidate_score_requires_retailer_and_rewards_matching_title():
    assert candidate_score("5PK YARD BAG", "homedepot.ca", "Kraft 5PK Yard Bag", "https://www.homedepot.ca/product/bags") > 0.85
    assert candidate_score("5PK YARD BAG", "homedepot.ca", "Kraft 5PK Yard Bag", "https://amp.homedepot.ca/product/bags") > 0.85
    assert candidate_score("5PK YARD BAG", "homedepot.ca", "Kraft Yard Bags", "https://example.com/bags") == 0


def test_candidate_score_does_not_accept_category_or_promotion_pages():
    assert candidate_score("OLAY BODY WASH 5.99 GP", "shoppersdrugmart.ca", "Buy Olay Body Wash", "https://www.shoppersdrugmart.ca/shop/olay/categories/body") < 0.85
    assert candidate_score("Save up to", "shoppersdrugmart.ca", "Buy Online", "https://www.shoppersdrugmart.ca/page/OnlinePickUp") == 0


def test_candidate_score_recognizes_sobeys_plural_products_path():
    assert candidate_score(
        "Old El Paso medium picante sauce",
        "sobeys.com",
        "Old El Paso Salsa Picante Style Restaurant Medium 650 ml",
        "https://www.sobeys.com/products/old-el-paso-salsa-picante-style-restaurant-medium-650-ml",
    ) > 0.85


def test_verified_product_initialism_corrects_one_ocr_glyph_only_at_high_confidence():
    title = "Old El Paso Salsa Picante Style Restaurant Medium 650 ml"
    assert normalized_item_name_from_verified_product("Cep Pic Med", title, 1.0) == "Oep Pic Med"
    assert normalized_item_name_from_verified_product("Cep Pic Med", title, 0.94) is None
    assert normalized_item_name_from_verified_product("Bad Pic Med", title, 1.0) is None


def test_ocr_initialism_and_abbreviations_have_explainable_product_evidence():
    evidence = product_enrichment.candidate_evidence(
        'Cep Pic Med', 'sobeys.com',
        'Old El Paso Salsa Picante Style Restaurant Medium 650 ml',
        'https://www.sobeys.com/products/old-el-paso-salsa-picante-style-restaurant-medium-650-ml')
    assert evidence['confidence'] == .9167
    assert [t['kind'] for t in evidence['tokens']] == [
        'ocr_brand_initialism', 'token_prefix', 'token_prefix']
    assert evidence['tokens'][0]['matched'] == 'oep'


def test_recipe_brand_page_and_interior_substrings_are_not_product_matches():
    assert candidate_score('Cep Pic Med', 'sobeys.com', 'Cepacol Medication',
                           'https://www.sobeys.com/brands/cepacol', 'Cep Pic Med') == 0
    assert candidate_score('Cep Pic Med', 'sobeys.com', 'Cep Pic Med',
                           'https://www.sobeys.com/recipes/slaw') == 0
    evidence = product_enrichment.candidate_evidence(
        'Cep Pic Med', 'sobeys.com', 'Cep Spice Medium', 'https://sobeys.com/products/cep-spice-medium')
    assert evidence['tokens'][1]['kind'] == 'unmatched'
    assert evidence['confidence'] < .85
    assert candidate_score('079594233699', 'homedepot.ca', 'Yard bags',
                           'https://homedepot.ca/product/079594233699') == 1


def test_unmatched_variant_cannot_reach_default_acceptance_threshold():
    assert candidate_score('Brand Organic Red Medium Picante Salsa Special', 'sobeys.com',
                           'Brand Organic Red Medium Picante Salsa Other',
                           'https://sobeys.com/products/salsa') < .85


def test_normal_enrichment_rechecks_stale_cache_and_scores_original_text(monkeypatch):
    import psycopg
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def commit(self): pass
        def execute(self, sql, params):
            self.sql = sql
            return self
        def fetchall(self):
            return [{'id': 1, 'item_name': 'Cep Pic Med', 'store_name': 'Sobeys'}]
        def fetchone(self):
            if 'FROM enrichment.product_cache' in self.sql:
                return {'product_description': 'Quick Pickled Slaw',
                        'product_url': 'https://sobeys.com/recipes/slaw', 'confidence': .99}
            return None
    monkeypatch.setattr(psycopg, 'connect', lambda *a, **kw: Connection())
    searches = []
    def search(key, query, original, domain):
        searches.append((query, original))
        if 'Old El Paso' not in query:
            return None
        title, url = 'Old El Paso Salsa Picante Medium', 'https://sobeys.com/products/salsa'
        return product_enrichment.SearchResult(title, url, '', candidate_score(original, domain, title, url))
    monkeypatch.setattr(product_enrichment, 'brave_search', search)
    monkeypatch.setattr(product_enrichment, 'ai_product_queries', lambda *a: ('Old El Paso Salsa Picante Medium',))
    stats = product_enrichment.run(dsn='unused', api_key='unused', limit=1, threshold=.85, write_db=False)
    assert stats['db_hits'] == 0 and stats['accepted'] == 1
    assert len(searches) == 2 and all(original == 'Cep Pic Med' for _, original in searches)
