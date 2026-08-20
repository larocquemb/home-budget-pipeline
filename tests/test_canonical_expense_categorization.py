from decimal import Decimal

from home_budget_pipeline.categorization.pipeline import (
    CategoryDecision,
    CategoryMappingPipeline,
    CategoryProvenance,
)
from home_budget_pipeline.categorization.processing import (
    CanonicalExpenseCategorizer,
    CanonicalExpenseItem,
    ExpenseCategorySplit,
)


class FakeStore:
    def __init__(self):
        self.items = {
            42: [
                CanonicalExpenseItem(1, 42, "Romaine", Decimal("6.99")),
                CanonicalExpenseItem(2, 42, "Odd Product 123", Decimal("10.00")),
                CanonicalExpenseItem(3, 42, "ZXQ-991 unfamiliar object", Decimal("4.25")),
            ]
        }
        self.decisions = {}
        self.commit_count = 0

    def load_items(self, expense_pk):
        return self.items.get(expense_pk, [])

    def save_decision(self, item_id, decision):
        self.decisions[item_id] = decision

    def load_splits(self, expense_pk):
        totals = {}
        counts = {}
        review = {}
        for item in self.items.get(expense_pk, []):
            decision = self.decisions[item.id]
            if decision.category is None:
                continue
            totals[decision.category] = totals.get(decision.category, Decimal("0")) + (
                item.line_total or Decimal("0")
            )
            counts[decision.category] = counts.get(decision.category, 0) + 1
            review[decision.category] = review.get(decision.category, False) or decision.requires_review
        return [
            ExpenseCategorySplit(
                expense_pk=expense_pk,
                category_name=category,
                category_amount=amount,
                item_count=counts[category],
                requires_review=review[category],
            )
            for category, amount in sorted(totals.items())
        ]

    def commit(self):
        self.commit_count += 1


def test_categorizes_each_canonical_item_and_aggregates_splits():
    def ai_fallback(description, source, merchant):
        assert source == "costco"
        assert merchant == "Costco"
        return CategoryDecision(
            category="Indoor Supplies",
            provenance=CategoryProvenance.AI,
            confidence=0.62,
            rationale="ambiguous household product",
            requires_review=True,
        )

    pipeline = CategoryMappingPipeline(
        learned_mappings={"Odd Product 123": "Outdoor Supplies"},
        ai_fallback=ai_fallback,
    )
    store = FakeStore()
    result = CanonicalExpenseCategorizer(pipeline, store).categorize_expense(
        42,
        source="costco",
        merchant="Costco",
    )

    assert store.commit_count == 1
    assert len(result.decisions) == 3

    assert store.decisions[1].category == "Groceries"
    assert store.decisions[1].provenance == CategoryProvenance.RULE

    assert store.decisions[2].category == "Outdoor Supplies"
    assert store.decisions[2].provenance == CategoryProvenance.LEARNED

    assert store.decisions[3].category == "Indoor Supplies"
    assert store.decisions[3].provenance == CategoryProvenance.AI
    assert store.decisions[3].confidence == 0.62
    assert store.decisions[3].requires_review is True

    assert result.requires_review is True
    assert [(split.category_name, split.category_amount) for split in result.splits] == [
        ("Groceries", Decimal("6.99")),
        ("Indoor Supplies", Decimal("4.25")),
        ("Outdoor Supplies", Decimal("10.00")),
    ]


def test_unresolved_item_is_persisted_but_excluded_from_category_splits():
    pipeline = CategoryMappingPipeline()
    store = FakeStore()

    result = CanonicalExpenseCategorizer(pipeline, store).categorize_expense(42)

    assert store.decisions[3].category is None
    assert store.decisions[3].provenance == CategoryProvenance.UNRESOLVED
    assert store.decisions[3].requires_review is True
    assert all(split.category_name != "Cash/Unknown" for split in result.splits)


def test_processor_is_keyed_by_canonical_expense_not_payment_account():
    pipeline = CategoryMappingPipeline()
    store = FakeStore()
    categorizer = CanonicalExpenseCategorizer(pipeline, store)

    first = categorizer.categorize_expense(42)
    second = categorizer.categorize_expense(42)

    assert first.expense_pk == second.expense_pk == 42
    assert len(first.decisions) == len(second.decisions) == 3
    assert store.commit_count == 2


def test_rejects_invalid_canonical_expense_key():
    pipeline = CategoryMappingPipeline()
    store = FakeStore()

    try:
        CanonicalExpenseCategorizer(pipeline, store).categorize_expense(0)
    except ValueError as exc:
        assert "expense_pk must be positive" in str(exc)
    else:
        raise AssertionError("expected ValueError")
