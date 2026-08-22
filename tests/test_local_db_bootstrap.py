from pathlib import Path


def test_local_database_reset_normalizes_postgres_inet_address():
    script = Path("scripts/rebuild_local_database.sh").read_text()
    assert "host(inet_server_addr())" in script
    assert "home_budget\\|127.0.0.1" in script
    assert "home_budget\\|::1" in script


def test_local_database_reset_uses_shared_k3s_bootstrap():
    script = Path("scripts/rebuild_local_database.sh").read_text()
    assert "scripts/stage_db_bootstrap.sh" in script
