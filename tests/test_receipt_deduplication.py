from datetime import datetime, timedelta
from decimal import Decimal

from home_budget_pipeline.receipts.deduplication import (
    DuplicateDisposition,
    ReceiptDuplicateScorer,
    ReceiptEvidenceFingerprint,
    ReceiptItemFingerprint,
)


def evidence(evidence_id, **overrides):
    base = dict(
        evidence_id=evidence_id,
        source_type="scanned",
        merchant="Costco",
        transaction_datetime=datetime(2026, 8, 10, 14, 30),
        total=Decimal("295.47"),
        receipt_id=None,
        order_id=None,
        card_last4="1234",
        items=(
            ReceiptItemFingerprint("1% Milk", Decimal("1"), Decimal("5.94")),
            ReceiptItemFingerprint("Blueberries", Decimal("1"), Decimal("8.99")),
            ReceiptItemFingerprint("Eggs", Decimal("1"), Decimal("18.89")),
        ),
    )
    base.update(overrides)
    return ReceiptEvidenceFingerprint(**base)


def test_exact_duplicate_from_identifier_and_receipt_features():
    scorer = ReceiptDuplicateScorer()
    left = evidence(1, receipt_id="COSTCO-123")
    right = evidence(2, source_type="electronic", receipt_id="costco123")

    result = scorer.score(left, right)

    assert result.disposition is DuplicateDisposition.EXACT
    assert "identifier_match" in result.reasons
    assert "items_near_exact" in result.reasons


def test_probable_cross_source_duplicate_can_tolerate_total_difference():
    scorer = ReceiptDuplicateScorer()
    left = evidence(1, order_id="INST-55", total=Decimal("329.64"), source_type="instacart")
    right = evidence(2, order_id="INST55", source_type="costco_e_receipt")

    result = scorer.score(left, right)

    assert result.disposition in {DuplicateDisposition.EXACT, DuplicateDisposition.PROBABLE}
    assert "identifier_match" in result.reasons
    assert result.item_similarity == 1.0


def test_same_merchant_same_day_without_items_or_identifiers_requires_review_or_distinct():
    scorer = ReceiptDuplicateScorer()
    left = evidence(1, items=(), card_last4=None)
    right = evidence(
        2,
        transaction_datetime=datetime(2026, 8, 10, 18, 0),
        total=Decimal("141.12"),
        items=(),
        card_last4=None,
    )

    result = scorer.score(left, right)

    assert result.disposition is DuplicateDisposition.DISTINCT


def test_item_overlap_supports_probable_duplicate_when_total_differs():
    scorer = ReceiptDuplicateScorer()
    right_items = (
        ReceiptItemFingerprint("1 percent milk", Decimal("1")),
        ReceiptItemFingerprint("Blueberries", Decimal("1")),
        ReceiptItemFingerprint("Eggs", Decimal("1")),
    )
    left = evidence(1, total=Decimal("329.64"), card_last4=None)
    right = evidence(2, total=Decimal("295.47"), items=right_items, card_last4=None)

    result = scorer.score(left, right)

    assert result.item_similarity >= 0.5
    assert result.disposition in {DuplicateDisposition.PROBABLE, DuplicateDisposition.REVIEW}


def test_card_and_close_time_strengthen_match():
    scorer = ReceiptDuplicateScorer()
    left = evidence(1, items=(), total=Decimal("100.00"))
    right = evidence(
        2,
        items=(),
        total=Decimal("100.00"),
        transaction_datetime=datetime(2026, 8, 10, 14, 33),
    )

    result = scorer.score(left, right)

    assert "card_match" in result.reasons
    assert "time_within_5m" in result.reasons
    assert result.score >= 60


def test_candidate_ranking_is_deterministic():
    scorer = ReceiptDuplicateScorer()
    target = evidence(1, receipt_id="ABC")
    strong = evidence(3, source_type="electronic", receipt_id="ABC")
    weak = evidence(2, total=Decimal("40.00"), items=(), card_last4=None)

    ranked = scorer.rank_candidates(target, [weak, strong])

    assert [result.right_evidence_id for result in ranked] == [3, 2]
