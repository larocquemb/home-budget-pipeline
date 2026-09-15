from pathlib import Path


def test_local_database_reset_requires_colima_loopback_target():
    script = Path("scripts/rebuild_local_database.sh").read_text()
    assert 'os.environ["DATABASE_URL"]' in script
    assert "home_budget\\|127.0.0.1\\|5433" in script
    assert "home_budget\\|localhost\\|5433" in script
    assert "home_budget\\|::1\\|5433" in script
    assert "home_budget\\|127.0.0.1\\|5432" not in script


def test_local_database_reset_verifies_connected_database():
    script = Path("scripts/rebuild_local_database.sh").read_text()
    assert 'SELECT current_database()' in script
    assert 'if [ "$database" != "home_budget" ]' in script


def test_local_database_reset_uses_shared_k3s_bootstrap():
    script = Path("scripts/rebuild_local_database.sh").read_text()
    assert "scripts/stage_db_bootstrap.sh" in script
