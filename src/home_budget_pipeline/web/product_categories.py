"""Resolve uncategorized items from accepted products without model calls."""

from ..categorization.logic import normalize_for_match
from ..categorization.pipeline import CategoryMappingPipeline, CategoryDecision, CategoryProvenance


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
                                    'approved category rule applied during product review')
    decision = CategoryMappingPipeline().categorize(description or item.get('item_name') or '',
                                                    source=item.get('source'), merchant=item.get('store_name'))
    if decision.category in active and decision.confidence is not None and decision.confidence >= .95:
        return decision
    return None


def apply_category(cur, item: dict, description: str):
    cur.execute('SELECT category_name FROM budget.expense_categories WHERE is_active=TRUE')
    active = {r['category_name'] for r in cur.fetchall()}
    cur.execute('''SELECT source,merchant,match_type,match_text,category_name
        FROM budget.expense_category_mappings WHERE is_active=TRUE AND is_approved=TRUE
        ORDER BY priority,id''')
    decision = category_for_product(item, description, cur.fetchall(), active)
    if decision is None:
        return None
    cur.execute('''UPDATE budget.expense_items SET budget_category=%s,category_source=%s,
        category_confidence=%s,category_rationale=%s,category_requires_review=FALSE,
        categorized_at=NOW(),updated_at=NOW() WHERE id=%s''',
        (decision.category, decision.provenance.value, decision.confidence,
         decision.rationale, item['id']))
    return {'old_category': item.get('budget_category'), 'new_category': decision.category,
            'rationale': decision.rationale, 'confidence': decision.confidence}
