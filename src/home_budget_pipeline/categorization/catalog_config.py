from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_catalog(path: str | Path) -> dict[str, Any]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if data.get("version") != 1:
        raise ValueError("Unsupported category catalog version")

    groups = data.get("groups") or []
    categories = data.get("categories") or []
    group_keys = {g["key"] for g in groups}

    if len(group_keys) != len(groups):
        raise ValueError("Duplicate category group key")

    names: set[str] = set()
    aliases: set[str] = set()
    for category in categories:
        name = category["name"]
        if name in names:
            raise ValueError(f"Duplicate category: {name}")
        names.add(name)
        if category["group"] not in group_keys:
            raise ValueError(f"Unknown group for category {name}: {category['group']}")
        for alias in category.get("aliases") or []:
            if alias in aliases or alias in names:
                raise ValueError(f"Duplicate category alias: {alias}")
            aliases.add(alias)

    return data


def _sql_text(value: Any) -> str:
    if value is None:
        return "NULL"
    return "'" + str(value).replace("'", "''") + "'"


def render_catalog_sql(catalog: dict[str, Any]) -> str:
    lines = ["BEGIN;"]

    for group in catalog["groups"]:
        lines.append(
            "INSERT INTO budget.category_groups "
            "(group_key, group_name, description, sort_order, is_active) VALUES "
            f"({_sql_text(group['key'])}, {_sql_text(group['name'])}, "
            f"{_sql_text(group.get('description'))}, {int(group.get('sort_order', 100))}, TRUE) "
            "ON CONFLICT (group_key) DO UPDATE SET "
            "group_name = EXCLUDED.group_name, description = EXCLUDED.description, "
            "sort_order = EXCLUDED.sort_order, is_active = TRUE;"
        )

    for index, category in enumerate(catalog["categories"], start=1):
        lines.append(
            "INSERT INTO budget.expense_categories "
            "(category_name, group_id, description, sort_order, is_active) SELECT "
            f"{_sql_text(category['name'])}, id, {_sql_text(category.get('description'))}, "
            f"{int(category.get('sort_order', index * 10))}, TRUE "
            "FROM budget.category_groups "
            f"WHERE group_key = {_sql_text(category['group'])} "
            "ON CONFLICT (category_name) DO UPDATE SET "
            "group_id = EXCLUDED.group_id, description = EXCLUDED.description, "
            "sort_order = EXCLUDED.sort_order, is_active = TRUE;"
        )
        for alias in category.get("aliases") or []:
            lines.append(
                "INSERT INTO budget.expense_category_aliases (alias_name, category_id) SELECT "
                f"{_sql_text(alias)}, id FROM budget.expense_categories "
                f"WHERE category_name = {_sql_text(category['name'])} "
                "ON CONFLICT (alias_name) DO UPDATE SET category_id = EXCLUDED.category_id;"
            )

    lines.append("COMMIT;")
    return "\n".join(lines) + "\n"


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Render category YAML as PostgreSQL bootstrap SQL")
    parser.add_argument("catalog")
    parser.add_argument("output")
    args = parser.parse_args()

    catalog = load_catalog(args.catalog)
    Path(args.output).write_text(render_catalog_sql(catalog), encoding="utf-8")


if __name__ == "__main__":
    main()
