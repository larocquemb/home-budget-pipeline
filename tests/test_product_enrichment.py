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
    assert candidate_score("OLAY BODY WASH 5.99 GP", "shoppersdrugmart.ca", "Buy Olay Body Wash", "https://www.shoppersdrugmart.ca/shop/olay/categories/body") == 0.8
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
