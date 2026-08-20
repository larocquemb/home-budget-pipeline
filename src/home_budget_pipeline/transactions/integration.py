"""Persistence and provenance-safe enrichment for KAN-78."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, time
from typing import Optional

from .reconciliation import MatchDecision, MatchOutcome, TransactionForMatching


@dataclass(frozen=True)
class EnrichmentResult:
    expense_pk: int
    transaction_id: int
    transaction_datetime_applied: bool
    payer_applied: bool


@dataclass
class PostgresTransactionReconciliationStore:
    connection: object

    def save_decision(self, decision: MatchDecision) -> None:
        sql = """
            INSERT INTO budget.transaction_expense_reconciliation (
                transaction_id, expense_pk, outcome, score,
                candidate_count, candidate_details, reconciled_at
            )
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, NOW())
            ON CONFLICT (transaction_id) DO UPDATE SET
                expense_pk = EXCLUDED.expense_pk,
                outcome = EXCLUDED.outcome,
                score = EXCLUDED.score,
                candidate_count = EXCLUDED.candidate_count,
                candidate_details = EXCLUDED.candidate_details,
                reconciled_at = NOW()
        """
        details = [
            {
                "expense_pk": candidate.candidate.expense_pk,
                "score": candidate.score,
                "amount_match": candidate.amount_match,
                "date_distance_days": candidate.date_distance_days,
                "merchant_similarity": candidate.merchant_similarity,
                "card_match": candidate.card_match,
                "account_match": candidate.account_match,
                "identifier_match": candidate.identifier_match,
            }
            for candidate in decision.candidates
        ]
        with self.connection.cursor() as cur:
            cur.execute(
                sql,
                (
                    decision.transaction_id,
                    decision.matched_expense_pk,
                    decision.outcome.value,
                    decision.score,
                    len(decision.candidates),
                    json.dumps(details, sort_keys=True),
                ),
            )
        self.connection.commit()

    def enrich_matched_expense(
        self,
        *,
        decision: MatchDecision,
        transaction: TransactionForMatching,
        payer: Optional[str],
    ) -> EnrichmentResult:
        if decision.outcome is not MatchOutcome.MATCHED or decision.matched_expense_pk is None:
            raise ValueError("only matched reconciliation decisions may enrich an expense")

        expense_pk = decision.matched_expense_pk
        select_sql = """
            SELECT transaction_datetime, payer
            FROM budget.expenses
            WHERE id = %s
        """
        update_sql = """
            UPDATE budget.expenses
            SET transaction_datetime = COALESCE(transaction_datetime, %s),
                payer = COALESCE(payer, %s)
            WHERE id = %s
        """
        provenance_sql = """
            INSERT INTO budget.expense_transaction_enrichment (
                expense_pk, transaction_id,
                transaction_datetime_applied, payer_applied,
                source_type, enriched_at
            )
            VALUES (%s, %s, %s, %s, 'financial_transaction', NOW())
            ON CONFLICT (expense_pk) DO UPDATE SET
                transaction_id = EXCLUDED.transaction_id,
                transaction_datetime_applied = EXCLUDED.transaction_datetime_applied,
                payer_applied = EXCLUDED.payer_applied,
                source_type = EXCLUDED.source_type,
                enriched_at = NOW()
        """

        with self.connection.cursor() as cur:
            cur.execute(select_sql, (expense_pk,))
            row = cur.fetchone()
            if row is None:
                raise LookupError(f"canonical expense not found: {expense_pk}")
            existing_datetime, existing_payer = row
            applied_datetime = existing_datetime is None
            applied_payer = existing_payer is None and bool(payer)
            derived_datetime = datetime.combine(transaction.transaction_date, time.min)
            cur.execute(update_sql, (derived_datetime, payer, expense_pk))
            cur.execute(
                provenance_sql,
                (
                    expense_pk,
                    transaction.transaction_id,
                    applied_datetime,
                    applied_payer,
                ),
            )
        self.connection.commit()
        return EnrichmentResult(
            expense_pk=expense_pk,
            transaction_id=transaction.transaction_id,
            transaction_datetime_applied=applied_datetime,
            payer_applied=applied_payer,
        )
