import json

from home_budget_pipeline.categorization.ai_fallback import OpenAICategoryFallback
from home_budget_pipeline.categorization.pipeline import CategoryProvenance


class _FakeResponse:
    def __init__(self, payload):
        self.output_text = json.dumps(payload)


class _FakeResponses:
    def __init__(self, payload):
        self.payload = payload

    def create(self, **kwargs):
        return _FakeResponse(self.payload)


class _FakeClient:
    def __init__(self, payload):
        self.responses = _FakeResponses(payload)


def test_ai_fallback_preserves_confidence_and_rationale():
    fallback = OpenAICategoryFallback(
        client=_FakeClient(
            {
                "category": "Indoor Supplies",
                "confidence": 0.87,
                "rationale": "Household cleaning or consumable supply",
            }
        )
    )

    decision = fallback("unfamiliar household widget", "costco", "Costco")

    assert decision.category == "Indoor Supplies"
    assert decision.provenance == CategoryProvenance.AI
    assert decision.confidence == 0.87
    assert decision.rationale == "Household cleaning or consumable supply"
    assert decision.requires_review is False


def test_low_confidence_ai_decision_requires_review():
    fallback = OpenAICategoryFallback(
        client=_FakeClient(
            {
                "category": "Misc Household",
                "confidence": 0.51,
                "rationale": "Ambiguous household purchase",
            }
        )
    )

    decision = fallback("ambiguous item", None, None)

    assert decision.category == "Misc Household"
    assert decision.confidence == 0.51
    assert decision.requires_review is True
