from home_budget_pipeline.web.receipt_app import _enrichment_html


def test_enrichment_html_renders_provider_confidence_link_and_query():
    html = _enrichment_html(
        {
            "enrichment": {
                "provider": "brave+ai",
                "status": "accepted",
                "confidence": 0.97,
                "candidate_title": "Old El Paso Salsa Picante Medium",
                "candidate_url": "https://www.sobeys.com/products/old-el-paso-salsa",
                "search_query": "site:sobeys.com Old El Paso Salsa Picante Medium",
            }
        }
    )

    assert "brave+ai" in html
    assert "accepted" in html
    assert "97.0%" in html
    assert "Old El Paso Salsa Picante Medium" in html
    assert "https://www.sobeys.com/products/old-el-paso-salsa" in html
    assert "site:sobeys.com Old El Paso Salsa Picante Medium" in html


def test_enrichment_html_marks_missing_result():
    assert "Not enriched" in _enrichment_html({})
