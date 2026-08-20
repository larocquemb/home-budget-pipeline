"""Effective-dated account and card ownership resolution for KAN-78."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable, Optional


@dataclass(frozen=True)
class AccountOwnership:
    account_id: int
    owner_name: str
    valid_from: date
    valid_to: Optional[date] = None


@dataclass(frozen=True)
class PaymentCardOwnership:
    card_id: int
    account_id: Optional[int]
    card_network: str
    card_last4: str
    owner_name: str
    valid_from: date
    valid_to: Optional[date] = None


@dataclass(frozen=True)
class OwnershipResolution:
    owner_name: str
    account_id: Optional[int]
    source: str


class OwnershipResolver:
    """Resolve payer/owner using effective-dated configuration records."""

    def __init__(
        self,
        account_ownerships: Iterable[AccountOwnership] = (),
        card_ownerships: Iterable[PaymentCardOwnership] = (),
    ) -> None:
        self._accounts = tuple(account_ownerships)
        self._cards = tuple(card_ownerships)

    def resolve(
        self,
        *,
        transaction_date: date,
        account_id: Optional[int] = None,
        card_network: Optional[str] = None,
        card_last4: Optional[str] = None,
    ) -> Optional[OwnershipResolution]:
        if card_last4:
            matches = [
                card
                for card in self._cards
                if card.card_last4 == card_last4
                and (card_network is None or card.card_network.lower() == card_network.lower())
                and self._active(transaction_date, card.valid_from, card.valid_to)
            ]
            match = self._single_match(matches, "payment card ownership")
            if match is not None:
                return OwnershipResolution(match.owner_name, match.account_id, "payment_card")

        if account_id is not None:
            matches = [
                owner
                for owner in self._accounts
                if owner.account_id == account_id
                and self._active(transaction_date, owner.valid_from, owner.valid_to)
            ]
            match = self._single_match(matches, "account ownership")
            if match is not None:
                return OwnershipResolution(match.owner_name, match.account_id, "account")

        return None

    @staticmethod
    def _active(on_date: date, valid_from: date, valid_to: Optional[date]) -> bool:
        return valid_from <= on_date and (valid_to is None or on_date <= valid_to)

    @staticmethod
    def _single_match(matches, label):
        if len(matches) > 1:
            raise ValueError(f"ambiguous {label}: overlapping effective-date records")
        return matches[0] if matches else None
