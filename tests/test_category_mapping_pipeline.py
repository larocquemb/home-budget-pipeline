from home_budget_pipeline.categorization.pipeline import (
    CategoryDecision,
    CategoryMappingPipeline,
    CategoryProvenance,
)


def test_verified_override_preserves_rule_provenance():
    pipeline = CategoryMappingPipeline()

    result = pipeline.categorize("Romaine")

    assert result.category == "Groceries"
    assert result.provenance == CategoryProvenance.RULE
    assert result.confidence == 1.0
    assert result.requires_review is False


def test_learned_mapping_is_used_before_ai():
    ai_calls = []

    def ai_fallback(description, source, merchant):
        ai_calls.append(description)
        return CategoryDecision(
            category="Cash/Unknown",
            provenance=CategoryProvenance.AI,
            confidence=0.70,
            rationale="AI guess",
            requires_review=True,
        )

    pipeline = CategoryMappingPipeline(
        learned_mappings={"Very Strange Widget": "Indoor Supplies"},
        ai_fallback=ai_fallback,
    )

    result = pipeline.categorize("VERY strange---widget")

    assert result.category == "Indoor Supplies"
    assert result.provenance == CategoryProvenance.LEARNED
    assert result.confidence == 1.0
    assert ai_calls == []


def test_unknown_item_does_not_silently_default_to_groceries():
    pipeline = CategoryMappingPipeline()

    result = pipeline.categorize("ZXQ-991 unfamiliar object")

    assert result.category is None
    assert result.provenance == CategoryProvenance.UNRESOLVED
    assert result.requires_review is True


def test_ai_is_only_used_after_deterministic_paths_fail():
    def ai_fallback(description, source, merchant):
        assert description == "ZXQ-991 unfamiliar object"
        assert merchant == "Example Merchant"
        return CategoryDecision(
            category="Indoor Supplies",
            provenance=CategoryProvenance.AI,
            confidence=0.82,
            rationale="model matched household supply",
            requires_review=False,
        )

    pipeline = CategoryMappingPipeline(ai_fallback=ai_fallback)

    result = pipeline.categorize(
        "ZXQ-991 unfamiliar object",
        merchant="Example Merchant",
    )

    assert result.category == "Indoor Supplies"
    assert result.provenance == CategoryProvenance.AI
    assert result.confidence == 0.82
    assert result.rationale == "model matched household supply"
    assert result.requires_review is False


def test_human_approval_becomes_reusable_learned_mapping():
    pipeline = CategoryMappingPipeline()

    manual = pipeline.approve_mapping("Odd Product 123", "Outdoor Supplies")
    reused = pipeline.categorize("odd product 123")

    assert manual.provenance == CategoryProvenance.MANUAL
    assert manual.category == "Outdoor Supplies"
    assert reused.provenance == CategoryProvenance.LEARNED
    assert reused.category == "Outdoor Supplies"


def test_empty_description_requires_review():
    pipeline = CategoryMappingPipeline()

    result = pipeline.categorize(" --- ")

    assert result.category is None
    assert result.requires_review is True
