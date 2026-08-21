import pytest
from fastapi import HTTPException

from home_budget_pipeline.web.app import _optional_bool_query, authenticated_identity, health


def test_health_is_public_for_kubernetes_probes():
    assert health() == {"status": "ok", "service": "ledger"}


def test_authenticated_identity_accepts_oauth_proxy_headers():
    identity = authenticated_identity(
        x_forwarded_user="user-id",
        x_forwarded_email="user@example.com",
        x_auth_request_user=None,
        x_auth_request_email=None,
    )

    assert identity == {"user": "user-id", "email": "user@example.com"}


def test_authenticated_identity_rejects_missing_identity():
    with pytest.raises(HTTPException) as exc:
        authenticated_identity(
            x_forwarded_user=None,
            x_forwarded_email=None,
            x_auth_request_user=None,
            x_auth_request_email=None,
        )

    assert exc.value.status_code == 401


def test_empty_optional_boolean_query_means_no_filter():
    assert _optional_bool_query("") is None
    assert _optional_bool_query("true") is True
    assert _optional_bool_query("false") is False
