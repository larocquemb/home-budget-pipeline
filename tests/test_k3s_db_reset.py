from pathlib import Path


def test_shared_bootstrap_stages_product_enrichment_cache():
    script = Path("scripts/stage_db_bootstrap.sh").read_text()
    assert "009-product-enrichment-cache.sql" in script
    assert "sql/migrations/product_enrichment_cache.sql" in script


def test_k3s_reset_is_guarded_and_uses_shared_bootstrap():
    script = Path("scripts/rebuild_k3s_database.sh").read_text()
    assert "CONFIRM_K3S_DB_RESET" in script
    assert '!= "REBUILD"' in script
    assert "scripts/stage_db_bootstrap.sh" in script
    assert "DROP SCHEMA IF EXISTS budget CASCADE" in script
    assert "budget.product_enrichment_cache" in script


def test_makefile_exposes_k3s_db_reset_target():
    makefile = Path("Makefile").read_text()
    assert "k3s-db-reset:" in makefile
    assert "rebuild_k3s_database.sh" in makefile
