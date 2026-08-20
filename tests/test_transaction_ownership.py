from datetime import date

import pytest

from home_budget_pipeline.transactions.ownership import (
    AccountOwnership,
    OwnershipResolver,
    PaymentCardOwnership,
)


def test_resolves_payment_card_owner_for_effective_date():
    resolver = OwnershipResolver(
        card_ownerships=[
            PaymentCardOwnership(1, 10, "Mastercard", "1234", "Paul", date(2026, 1, 1))
        ]
    )
    result = resolver.resolve(
        transaction_date=date(2026, 8, 20),
        card_network="mastercard",
        card_last4="1234",
    )
    assert result.owner_name == "Paul"
    assert result.account_id == 10
    assert result.source == "payment_card"


def test_card_replacement_or_ownership_change_uses_effective_dates():
    resolver = OwnershipResolver(
        card_ownerships=[
            PaymentCardOwnership(1, 10, "Visa", "1111", "Paul", date(2025, 1, 1), date(2026, 3, 31)),
            PaymentCardOwnership(2, 10, "Visa", "2222", "Paul", date(2026, 4, 1)),
        ]
    )
    assert resolver.resolve(
        transaction_date=date(2026, 3, 15), card_network="Visa", card_last4="1111"
    ).owner_name == "Paul"
    assert resolver.resolve(
        transaction_date=date(2026, 8, 20), card_network="Visa", card_last4="2222"
    ).owner_name == "Paul"
    assert resolver.resolve(
        transaction_date=date(2026, 8, 20), card_network="Visa", card_last4="1111"
    ) is None


def test_falls_back_to_account_owner_when_card_does_not_resolve():
    resolver = OwnershipResolver(
        account_ownerships=[AccountOwnership(10, "Roxanne", date(2026, 1, 1))]
    )
    result = resolver.resolve(transaction_date=date(2026, 8, 20), account_id=10)
    assert result.owner_name == "Roxanne"
    assert result.source == "account"


def test_returns_none_outside_effective_account_dates():
    resolver = OwnershipResolver(
        account_ownerships=[AccountOwnership(10, "Paul", date(2025, 1, 1), date(2025, 12, 31))]
    )
    assert resolver.resolve(transaction_date=date(2026, 1, 1), account_id=10) is None


def test_overlapping_card_ownership_is_rejected_as_ambiguous():
    resolver = OwnershipResolver(
        card_ownerships=[
            PaymentCardOwnership(1, 10, "Visa", "1234", "Paul", date(2026, 1, 1)),
            PaymentCardOwnership(2, 10, "Visa", "1234", "Roxanne", date(2026, 6, 1)),
        ]
    )
    with pytest.raises(ValueError, match="ambiguous payment card ownership"):
        resolver.resolve(
            transaction_date=date(2026, 8, 20), card_network="Visa", card_last4="1234"
        )
