"""Persistence adapters for approved category mappings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Protocol

from .catalog import VALID_CATEGORIES, canonicalize_category
from .logic import normalize_for_match


class CategoryMappingStore(Protocol):
    def load_approved_mappings(self) -> Dict[str, str]: ...

    def save_approved_mapping(
        self,
        description: str,
        category: str,
        *,
        source: Optional[str] = None,
        merchant: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> None: ...


@dataclass
class PostgresCategoryMappingStore:
    """PostgreSQL-backed store for approved exact product mappings."""

    connection: object

    def load_approved_mappings(self) -> Dict[str, str]:
        sql = """
            SELECT match_text, category_name
            FROM budget.expense_category_mappings
            WHERE is_active = TRUE
              AND is_approved = TRUE
              AND provenance IN ('manual', 'learned_mapping')
              AND match_type = 'exact'
            ORDER BY priority, id
        """
        mappings: Dict[str, str] = {}
        with self.connection.cursor() as cur:
            cur.execute(sql)
            for match_text, raw_category in cur.fetchall():
                normalized = normalize_for_match(str(match_text or ""))
                category = canonicalize_category(str(raw_category or ""))
                if normalized and category in VALID_CATEGORIES:
                    mappings[normalized] = category
        return mappings

    def save_approved_mapping(
        self,
        description: str,
        category: str,
        *,
        source: Optional[str] = None,
        merchant: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> None:
        normalized = normalize_for_match(description)
        canonical = canonicalize_category(category)
        if not normalized:
            raise ValueError("description must not normalize to an empty value")
        if canonical not in VALID_CATEGORIES:
            raise ValueError(f"invalid category: {category}")

        sql = """
            INSERT INTO budget.expense_category_mappings (
                source, merchant, match_type, match_text, category_name,
                priority, is_active, provenance, is_approved, notes
            )
            VALUES (%s, %s, 'exact', %s, %s, 10, TRUE, 'manual', TRUE, %s)
            ON CONFLICT (
                (COALESCE(lower(trim(source)), '')),
                (COALESCE(lower(trim(merchant)), '')),
                match_type,
                (lower(trim(match_text)))
            )
            DO UPDATE SET
                category_name = EXCLUDED.category_name,
                priority = EXCLUDED.priority,
                is_active = TRUE,
                provenance = 'manual',
                is_approved = TRUE,
                notes = COALESCE(EXCLUDED.notes, budget.expense_category_mappings.notes),
                updated_at = NOW()
        """
        with self.connection.cursor() as cur:
            cur.execute(sql, (source, merchant, normalized, canonical, notes))
        self.connection.commit()
