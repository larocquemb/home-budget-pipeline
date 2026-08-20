"""Canonical line-item category mapping pipeline.

The pipeline deliberately separates category *decisions* from the legacy helpers that
return a default category. An item that cannot be categorized deterministically must
remain unresolved so it can be sent to AI or human review instead of being silently
classified as Groceries.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Mapping, Optional

from .logic import (
    CATEGORY_ORDER,
    VERIFIED_CATEGORY_OVERRIDES,
    canonicalize_category,
    match_runtime_category_rule,
    normalize_for_match,
    try_exact_keyword_category,
    try_fuzzy_keyword_category,
)
from .store import CategoryMappingStore


class CategoryProvenance(str, Enum):
    """How a category assignment was produced."""

    RULE = "rule"
    LEARNED = "learned_mapping"
    AI = "ai"
    MANUAL = "manual"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class CategoryDecision:
    """Auditable result of categorizing one canonical receipt line item."""

    category: Optional[str]
    provenance: CategoryProvenance
    confidence: Optional[float]
    rationale: str
    requires_review: bool = False

    @property
    def resolved(self) -> bool:
        return self.category in CATEGORY_ORDER


AIFallback = Callable[[str, Optional[str], Optional[str]], CategoryDecision]


class CategoryMappingPipeline:
    """Deterministic-first category resolver for canonical receipt line items.

    Processing order mirrors KAN-68:
      1. normalize product description
      2. explicit deterministic rules / verified overrides
      3. previously approved (learned) mappings
      4. keyword/fuzzy deterministic rules
      5. optional AI fallback
      6. human review when still unresolved or low confidence
    """

    def __init__(
        self,
        learned_mappings: Optional[Mapping[str, str]] = None,
        ai_fallback: Optional[AIFallback] = None,
        mapping_store: Optional[CategoryMappingStore] = None,
    ) -> None:
        self.mapping_store = mapping_store
        combined = dict(learned_mappings or {})
        if mapping_store is not None:
            combined.update(mapping_store.load_approved_mappings())
        self.learned_mappings = {
            normalize_for_match(key): category
            for key, raw_category in combined.items()
            if (category := canonicalize_category(raw_category)) in CATEGORY_ORDER
        }
        self.ai_fallback = ai_fallback

    def categorize(
        self,
        description: str,
        *,
        source: Optional[str] = None,
        merchant: Optional[str] = None,
    ) -> CategoryDecision:
        normalized = normalize_for_match(description)
        if not normalized:
            return CategoryDecision(
                category=None,
                provenance=CategoryProvenance.UNRESOLVED,
                confidence=None,
                rationale="empty normalized description",
                requires_review=True,
            )

        verified = canonicalize_category(VERIFIED_CATEGORY_OVERRIDES.get(normalized))
        if verified in CATEGORY_ORDER:
            return CategoryDecision(
                category=verified,
                provenance=CategoryProvenance.RULE,
                confidence=1.0,
                rationale="verified product override",
            )

        runtime = canonicalize_category(
            match_runtime_category_rule(description, source=source, merchant=merchant)
        )
        if runtime in CATEGORY_ORDER:
            return CategoryDecision(
                category=runtime,
                provenance=CategoryProvenance.RULE,
                confidence=1.0,
                rationale="configured category mapping rule",
            )

        learned = canonicalize_category(self.learned_mappings.get(normalized))
        if learned in CATEGORY_ORDER:
            return CategoryDecision(
                category=learned,
                provenance=CategoryProvenance.LEARNED,
                confidence=1.0,
                rationale="previously approved product mapping",
            )

        exact = canonicalize_category(try_exact_keyword_category(description))
        if exact in CATEGORY_ORDER:
            return CategoryDecision(
                category=exact,
                provenance=CategoryProvenance.RULE,
                confidence=0.95,
                rationale="deterministic keyword rule",
            )

        fuzzy = canonicalize_category(try_fuzzy_keyword_category(description))
        if fuzzy in CATEGORY_ORDER:
            return CategoryDecision(
                category=fuzzy,
                provenance=CategoryProvenance.RULE,
                confidence=0.80,
                rationale="deterministic fuzzy keyword rule",
            )

        if self.ai_fallback is not None:
            ai_decision = self.ai_fallback(description, source, merchant)
            ai_category = canonicalize_category(ai_decision.category)
            if ai_category in CATEGORY_ORDER:
                return CategoryDecision(
                    category=ai_category,
                    provenance=CategoryProvenance.AI,
                    confidence=ai_decision.confidence,
                    rationale=ai_decision.rationale,
                    requires_review=ai_decision.requires_review,
                )

        return CategoryDecision(
            category=None,
            provenance=CategoryProvenance.UNRESOLVED,
            confidence=None,
            rationale="no deterministic or AI mapping",
            requires_review=True,
        )

    def approve_mapping(
        self,
        description: str,
        category: str,
        *,
        source: Optional[str] = None,
        merchant: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> CategoryDecision:
        """Apply a human correction and persist it for future pipeline runs."""
        normalized = normalize_for_match(description)
        canonical = canonicalize_category(category)
        if not normalized:
            raise ValueError("description must not normalize to an empty value")
        if canonical not in CATEGORY_ORDER:
            raise ValueError(f"invalid category: {category}")

        if self.mapping_store is not None:
            self.mapping_store.save_approved_mapping(
                description,
                canonical,
                source=source,
                merchant=merchant,
                notes=notes,
            )
        self.learned_mappings[normalized] = canonical
        return CategoryDecision(
            category=canonical,
            provenance=CategoryProvenance.MANUAL,
            confidence=1.0,
            rationale="human-approved category mapping",
        )
