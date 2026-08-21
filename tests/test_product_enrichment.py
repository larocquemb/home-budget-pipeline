from home_budget_pipeline.product_enrichment import candidate_score, product_query, retailer_domain


def test_retailer_query_prefers_barcode_and_domain():
    assert retailer_domain("The Home Depot") == "homedepot.ca"
    assert product_query("079594233699 5PK YARD BAG <A>", "The Home Depot") == 'site:homedepot.ca "079594233699"'


def test_retailer_query_removes_receipt_price_and_tax_suffix_without_exact_phrase():
    assert product_query("DOVE CLINL ~R0 9.99 GP", "Shoppers Drug Mart") == "site:shoppersdrugmart.ca DOVE CLINL R0"


def test_candidate_score_requires_retailer_and_rewards_matching_title():
    assert candidate_score("5PK YARD BAG", "homedepot.ca", "Kraft 5PK Yard Bag", "https://www.homedepot.ca/product/bags") > 0.85
    assert candidate_score("5PK YARD BAG", "homedepot.ca", "Kraft 5PK Yard Bag", "https://amp.homedepot.ca/product/bags") > 0.85
    assert candidate_score("5PK YARD BAG", "homedepot.ca", "Kraft Yard Bags", "https://example.com/bags") == 0
