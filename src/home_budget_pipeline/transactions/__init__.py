"""Financial transaction import, normalization, ownership, reconciliation, and integration."""

from .csv_import import FinancialTransaction, TransactionCsvNormalizer, transaction_fingerprint
from .integration import EnrichmentResult, PostgresTransactionReconciliationStore
from .ownership import (
    AccountOwnership,
    OwnershipResolution,
    OwnershipResolver,
    PaymentCardOwnership,
)
from .reconciliation import (
    CandidateScore,
    MatchCandidate,
    MatchDecision,
    MatchOutcome,
    ReconciliationSummary,
    TransactionForMatching,
    TransactionReceiptMatcher,
)

__all__ = [
    "FinancialTransaction",
    "TransactionCsvNormalizer",
    "transaction_fingerprint",
    "EnrichmentResult",
    "PostgresTransactionReconciliationStore",
    "AccountOwnership",
    "PaymentCardOwnership",
    "OwnershipResolution",
    "OwnershipResolver",
    "CandidateScore",
    "MatchCandidate",
    "MatchDecision",
    "MatchOutcome",
    "ReconciliationSummary",
    "TransactionForMatching",
    "TransactionReceiptMatcher",
]
