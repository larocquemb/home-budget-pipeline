"""Assign printed departments using retained geometry, without accounting categories."""
import copy
import re
from decimal import Decimal, InvalidOperation

HEADING = re.compile(r'^(?:BAKERY|DELI|DAIRY|FROZEN(?: FOODS?)?|GROCERY|MEAT|PRODUCE|SEAFOOD|PHARMACY|HOUSEHOLD)$', re.I)
SUMMARY = re.compile(r'\b(?:SUB\s*TOTAL|TOTAL|TENDER|PAYMENT)\b', re.I)


def walk(lines):
    for line in lines:
        yield line
        yield from walk(line.get('children', []))


def label_pages(pages):
    """Re-evaluate legacy cached labels; page boundaries never inherit coordinates."""
    result = copy.deepcopy(pages)
    for page in result:
        heading = None
        for line in walk(page.get('lines', [])):
            text = line.get('text', '').strip()
            bounds = (line.get('layout') or {}).get('bounds') or line.get('bounds') or {}
            x = bounds.get('x')
            line['department'] = None
            if HEADING.fullmatch(text):
                heading = (text.upper(), bounds) if isinstance(x, (int, float)) else None
            elif SUMMARY.search(text):
                heading = None
            elif heading and isinstance(x, (int, float)):
                name, anchor = heading
                tolerance = max(12, 0.75 * max(bounds.get('height') or 0, anchor.get('height') or 0))
                if x < anchor['x'] - tolerance:
                    heading = None
                else:
                    line['department'] = name
    return result


def assign_items(items, pages):
    """Only transfer unambiguous source item readings; keep monetary parsing unchanged."""
    from .ingest import extract_items
    candidates = {}
    for page in label_pages(pages):
        blocks = []
        for line in walk(page.get('lines', [])):
            department = line.get('department')
            if not blocks or blocks[-1][0] != department:
                blocks.append((department, []))
            blocks[-1][1].append(line.get('text', ''))
        for department, text in blocks:
            for parsed in extract_items('\n'.join(text)):
                key = (parsed.item_name, round(parsed.line_total, 2))
                candidates.setdefault(key, set()).add(department)
    for item in items:
        key = (item.item_name, round(item.line_total, 2))
        values = candidates.get(key, set())
        if not values and item.line_total < 0:
            values = candidates.get((item.item_name, round(abs(item.line_total), 2)), set())
        item.department = next(iter(values)) if len(values) == 1 else None
    return items


def item_department(item, source_items):
    """Join retained extraction to canonical items conservatively, including after edits."""
    values = set()
    for source in source_items or ():
        if not isinstance(source, dict) or source.get('item_name') != item.get('item_name'):
            continue
        try:
            if Decimal(str(source.get('line_total'))) != Decimal(str(item.get('line_total'))):
                continue
        except InvalidOperation:
            continue
        values.add(source.get('department'))
    return next(iter(values)) if len(values) == 1 else None
