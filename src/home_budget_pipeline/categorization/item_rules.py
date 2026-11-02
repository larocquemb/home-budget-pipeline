"""Shared deterministic rules for receipt persistence and product review."""

from .logic import normalize_for_match
from .pipeline import CategoryMappingPipeline, CategoryDecision, CategoryProvenance


def category_for_product(item: dict, description: str, rules: list[dict], active: set[str]):
    # Established categories, including human-approved rules, stay authoritative.
    if item.get('budget_category') not in {None, '', 'Uncategorized'}:
        return None
    if item.get('category_source') == 'manual' or item.get('category_rationale') == 'human-approved category override':
        return None
    names = [normalize_for_match(item.get('item_name') or ''), normalize_for_match(description)]
    for rule in rules:
        if rule.get('source') and normalize_for_match(rule['source']) != normalize_for_match(item.get('source') or ''):
            continue
        if rule.get('merchant') and normalize_for_match(rule['merchant']) != normalize_for_match(item.get('store_name') or ''):
            continue
        match = normalize_for_match(rule['match_text'])
        if not match or rule['category_name'] not in active:
            continue
        if any(name == match if rule['match_type'] == 'exact' else match in name for name in names):
            return CategoryDecision(rule['category_name'], CategoryProvenance.RULE, 1.0,
                                    'approved category rule')
    decision = CategoryMappingPipeline().categorize(description or item.get('item_name') or '',
                                                    source=item.get('source'), merchant=item.get('store_name'))
    if decision.category in active and decision.confidence is not None and decision.confidence >= .95:
        return decision
    # Section is supporting printed context, not itself an accounting category.
    # Item/accepted-product decisions take precedence over this fallback.
    section = item.get('department') or ''
    if section:
        decision = CategoryMappingPipeline().categorize(section,
            source=item.get('source'), merchant=item.get('store_name'))
        if decision.category in active and decision.confidence is not None and decision.confidence >= .95:
            return CategoryDecision(decision.category, decision.provenance, decision.confidence,
                                    'printed section supports ' + decision.rationale)
    return None


def apply_category(cur, item: dict, description: str, *, schema: str = 'budget'):
    import re
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', schema):
        raise ValueError('invalid database schema')
    cur.execute(f'SELECT category_name FROM {schema}.expense_categories WHERE is_active=TRUE')
    active = {r['category_name'] if isinstance(r, dict) else r[0] for r in cur.fetchall()}
    cur.execute(f'''SELECT source,merchant,match_type,match_text,category_name
        FROM {schema}.expense_category_mappings WHERE is_active=TRUE AND is_approved=TRUE
        ORDER BY priority,id''')
    fields = ('source', 'merchant', 'match_type', 'match_text', 'category_name')
    rules = [dict(row) if isinstance(row, dict) else dict(zip(fields, row)) for row in cur.fetchall()]
    decision = category_for_product(item, description, rules, active)
    if decision is None:
        return None
    cur.execute(f'''UPDATE {schema}.expense_items SET budget_category=%s,category_source=%s,
        category_confidence=%s,category_rationale=%s,category_requires_review=FALSE,
        categorized_at=NOW(),updated_at=NOW() WHERE id=%s''',
        (decision.category, decision.provenance.value, decision.confidence,
         decision.rationale, item['id']))
    return {'old_category': item.get('budget_category'), 'new_category': decision.category,
            'rationale': decision.rationale, 'confidence': decision.confidence}
