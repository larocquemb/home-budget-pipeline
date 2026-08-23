import inspect

from home_budget_pipeline import product_enrichment
from home_budget_pipeline.product_enrichment import normalized_cache_item_name


def test_product_enrichment_checks_persistent_cache_before_external_search():
    source = inspect.getsource(product_enrichment.run)
    cache_lookup = source.index("FROM budget.product_enrichment_cache")
    brave_lookup = source.index("brave_search(api_key")
    ai_lookup = source.index("ai_product_queries(*expansion_key)")
    assert cache_lookup < brave_lookup < ai_lookup
    assert "stats[\"db_hits\"] += 1" in source


def test_product_enrichment_cache_is_merchant_and_receipt_text_scoped():
    source = inspect.getsource(product_enrichment.run)
    assert "merchant_key=%s AND item_name_norm=%s" in source
    assert "ON CONFLICT (merchant_key, item_name_norm) DO UPDATE" in source
    assert normalized_cache_item_name("  Cep Pic Med  ") == "cep pic med"
    assert normalized_cache_item_name("CEP-PIC/MED") == "cep pic med"
