from home_budget_pipeline.web.receipt_app import (
    BASE_PATH,
    _receipt_detail,
    app,
    receipt_url,
)


def test_receipt_url_uses_stable_source_hash():
    source_sha256 = "a" * 64
    assert receipt_url(source_sha256) == f"{BASE_PATH}/receipts/{source_sha256}"


def test_receipt_detail_rejects_non_sha_identity_without_querying_database():
    class Service:
        def _fetch(self, *_args, **_kwargs):
            raise AssertionError("database should not be queried for an invalid receipt key")

    assert _receipt_detail(Service(), "532") is None


def test_receipt_first_routes_are_registered():
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert f"{BASE_PATH}/receipts" in paths
    assert f"{BASE_PATH}/receipts/{{source_sha256}}" in paths
    assert f"{BASE_PATH}/api/receipts/{{source_sha256}}" in paths
    assert f"{BASE_PATH}/dashboard" in paths
