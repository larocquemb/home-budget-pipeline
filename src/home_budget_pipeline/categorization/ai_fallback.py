"""Auditable AI fallback for canonical category decisions.

This adapter is intentionally small and stateless. Approved human corrections belong in
PostgreSQL learned mappings; AI output is retained on the expense item as provenance,
confidence, and rationale.
"""

from __future__ import annotations

import json
import os
from typing import Optional

from .catalog import CATEGORY_ORDER, VALID_CATEGORIES, canonicalize_category
from .pipeline import CategoryDecision, CategoryProvenance


class OpenAICategoryFallback:
    """Classify unknown line items using the configured Ledger category catalogue."""

    def __init__(self, client: object | None = None) -> None:
        self.enabled = os.getenv("ENABLE_AI_CATEGORY_FALLBACK", "1").lower() in {
            "1",
            "true",
            "yes",
        }
        self.confidence_threshold = float(os.getenv("AI_CONFIDENCE_THRESHOLD", "0.65"))
        self.model = os.getenv("AI_CATEGORY_MODEL", "gpt-4o-mini")
        self.timeout_seconds = float(os.getenv("AI_TIMEOUT_SECONDS", "12"))
        self._client = client

    def _client_or_none(self):
        if not self.enabled:
            return None
        if self._client is not None:
            return self._client
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            return None
        try:
            from openai import OpenAI
        except ImportError:
            return None
        self._client = OpenAI(api_key=api_key, timeout=self.timeout_seconds, max_retries=1)
        return self._client

    def __call__(
        self,
        description: str,
        source: Optional[str],
        merchant: Optional[str],
    ) -> CategoryDecision:
        client = self._client_or_none()
        if client is None:
            return CategoryDecision(
                category=None,
                provenance=CategoryProvenance.AI,
                confidence=None,
                rationale="AI fallback unavailable or disabled",
                requires_review=True,
            )

        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["category", "confidence", "rationale"],
            "properties": {
                "category": {"type": "string", "enum": list(CATEGORY_ORDER)},
                "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "rationale": {"type": "string"},
            },
        }
        context = []
        if source:
            context.append(f"source={source}")
        if merchant:
            context.append(f"merchant={merchant}")
        context_text = ", ".join(context) if context else "no merchant/source context"
        prompt = (
            "Classify this household purchase line item into exactly one allowed budget category.\n"
            f"Allowed categories: {', '.join(CATEGORY_ORDER)}.\n"
            f"Context: {context_text}.\n"
            f"Item: {description}\n"
            "Return a concise rationale and a confidence from 0 to 1."
        )

        try:
            response = client.responses.create(
                model=self.model,
                input=prompt,
                temperature=0,
                max_output_tokens=180,
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "budget_category_decision",
                        "strict": True,
                        "schema": schema,
                    }
                },
            )
            payload = json.loads((response.output_text or "").strip())
            category = canonicalize_category(payload.get("category"))
            confidence = float(payload.get("confidence", 0.0))
            rationale = str(payload.get("rationale") or "AI category classification").strip()
            if category not in VALID_CATEGORIES:
                raise ValueError("AI returned an invalid category")
            return CategoryDecision(
                category=category,
                provenance=CategoryProvenance.AI,
                confidence=confidence,
                rationale=rationale,
                requires_review=confidence < self.confidence_threshold,
            )
        except Exception as exc:
            return CategoryDecision(
                category=None,
                provenance=CategoryProvenance.AI,
                confidence=None,
                rationale=f"AI fallback failed: {type(exc).__name__}",
                requires_review=True,
            )
