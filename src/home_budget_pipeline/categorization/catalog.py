"""Runtime access to the configured Ledger category catalogue."""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

from .catalog_config import load_catalog


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def catalog_path() -> Path:
    configured = os.getenv("HOME_BUDGET_CATEGORY_CATALOG")
    if configured:
        return Path(configured)

    candidates = [
        Path.cwd() / "config" / "categories.yaml",
        Path(__file__).resolve().parents[3] / "config" / "categories.yaml",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        "Category catalogue not found; set HOME_BUDGET_CATEGORY_CATALOG"
    )


@lru_cache(maxsize=1)
def _catalog() -> dict:
    return load_catalog(catalog_path())


def reload_catalog() -> None:
    _catalog.cache_clear()


@lru_cache(maxsize=1)
def category_order() -> tuple[str, ...]:
    return tuple(str(category["name"]) for category in _catalog()["categories"])


@lru_cache(maxsize=1)
def category_aliases() -> dict[str, str]:
    aliases: dict[str, str] = {}
    for category in _catalog()["categories"]:
        canonical = str(category["name"])
        aliases[_normalize(canonical)] = canonical
        for alias in category.get("aliases") or []:
            aliases[_normalize(str(alias))] = canonical
    return aliases


def canonicalize_category(category: Optional[str]) -> Optional[str]:
    if not category:
        return None
    return category_aliases().get(_normalize(category))


def is_valid_category(category: Optional[str]) -> bool:
    canonical = canonicalize_category(category)
    return canonical is not None and canonical in category_order()


CATEGORY_ORDER = category_order()
VALID_CATEGORIES = frozenset(CATEGORY_ORDER)
