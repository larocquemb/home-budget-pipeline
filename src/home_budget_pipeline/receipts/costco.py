#!/usr/bin/env python3
"""
Costco receipt extractor with simplified budget categories and product hyperlinks.

Reads Costco PDF receipts from a ZIP archive or PDF path, extracts item-level rows, infers SKUs when
present in the receipt text, assigns one of these categories:

- Groceries
- Health & Fitness
- Clothing
- Indoor Supplies
- Outdoor Supplies

It also builds a clickable Costco Canada search URL for each SKU or item description.

Default input:
    /Users/paul/Documents/Finance/Budget/2026/Groceries/Archive.zip

Default outputs:
    /Users/paul/Documents/Finance/Budget/2026/Groceries/costco_receipt_items_categorized.xlsx
    /Users/paul/Documents/Finance/Budget/2026/Groceries/costco_receipt_extract_with_categories.py
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import time
import zipfile
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote_plus

import pdfplumber
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from budget_category_logic import CATEGORY_ORDER, DEFAULT_CATEGORY, deterministic_category, set_runtime_category_rules
from budget_category_ai import AICategoryEngine

PROJECT_DIR = Path(__file__).resolve().parent
ZIP_PATH = PROJECT_DIR / 'Archive.zip'
EXTRACT_DIR = PROJECT_DIR / 'costco_receipts_extracted_categorized'
OUT_XLSX = PROJECT_DIR / 'costco_receipt_items_categorized.xlsx'
AI_SUGGESTIONS_PATH = PROJECT_DIR / 'ai_category_cache.json'

AMOUNT_RE = re.compile(r'(\$?-?\d+\.\d{2}-?)\s*(?:[A-Z])?\s*$')
DATE_NAME_RE = re.compile(r'^(\d{8}[a-z]?)\.pdf$', re.I)
SKU_RE = re.compile(r'\b(\d{3,8})\b')
LONG_NUM_RE = re.compile(r'\b(\d{20,30})\b')
LONG_NUM_FUZZY_RE = re.compile(r'((?:\d[\s\-]*){20,36})')

SKIP_PATTERNS = [
    r'\bSUBTOTAL\b',
    r'\bTOTAL\b',
    r'\bTAX\b',
    r'\bGST\b',
    r'\bPST\b',
    r'\bHST\b',
    r'\bMASTERCARD\b',
    r'\bVISA\b',
    r'\bDEBIT\b',
    r'\bCHANGE\b',
    r'\bCASH\b',
    r'\bTHANK\b',
    r'\bMEMBER\b',
    r'\bWAREHOUSE\b',
    r'\bCOSTCO\b',
    r'\bITEM COUNT\b',
    r'\bYOU SAVED\b',
    r'\bBALANCE\b',
    r'\bAUTH\b',
    r'\bAPPROVED\b',
    r'\bTERMINAL\b',
    r'\bTRANSACTION\b',
    r'\bCARD\b',
    r'\bAMOUNT DUE\b',
    r'^\s*AMOUNT:\s*\$?',
    r'\bINSTANT SAVINGS\b',
]
SKIP_RE = re.compile('|'.join(SKIP_PATTERNS), re.I)

CATEGORY_KEYWORDS = {
    'Medical Products': [
        'readers', 'reading glasses', 'glasses', 'cetaphil', 'bandage', 'bandages',
        'first aid', 'pain relief', 'ibuprofen', 'acetaminophen', 'tylenol',
        'advil', 'allergy', 'cold', 'flu', 'cough'
    ],
    'Health & Fitness': [
        'protein powder', 'protein powders', 'electrolyte', 'electrolytes',
        'electrolyte mix', 'electrolyte mixes', 'vitamin', 'vitamins',
        'omega', 'creatine', 'supplement', 'supplements', 'workout supplement',
        'workout supplements', 'gummies', 'multivitamin',
        'protein shake', 'protein shakes', 'shake', 'powder', 'collagen', 'magnesium', 'wakewater', 'built puff',
        'quest', 'fairlife', 'premier protein'
    ],
    'Clothing': [
        'shirt', 'pant', 'pants', 'jeans', 'jacket', 'coat', 'hoodie', 'sock', 'socks', 'underwear',
        'brief', 'boxer', 'bra', 'shoe', 'shoes', 'boot', 'boots', 'slipper', 'hat',
        'glove', 'gloves', 'sweater', 'legging', 'leggings', 'short', 'shorts', 'dress', 'pajama',
        'tee', 't-shirt', 'parka', 'fleece', 'crew', 'crewneck', 'polo', 'lounge', 'dkny', 'clothing',
        'mondetta', 'vest', 'top', 'adidas', 'tshirt', 'tee shirt', 'tshrit', 'thong', 'tankini', 'swimwear',
        'knix'
    ],
    'Indoor Supplies': [
        'paper towel', 'toilet paper', 'tissue', 'napkin', 'detergent', 'laundry',
        'dish soap', 'dishwasher', 'soap', 'cleaner', 'cleaning', 'garbage bag', 'garbage bags',
        'kitchen bag', 'ziplock', 'foil', 'plastic wrap', 'parchment', 'sponge', 'sponges',
        'batteries', 'battery', 'light bulb', 'lightbulb', 'bulb', 'bulbs', 'kleenex',
        'wipes', 'sanitizer', 'disinfect', 'trash bag', 'trash bags', 'plate', 'plates', 'bounty'
    ],
    'Outdoor Supplies': [
        'soil', 'mulch', 'garden', 'planter', 'hose', 'bbq', 'propane', 'charcoal', 'patio',
        'deck', 'outdoor', 'lawn', 'grass seed', 'fertilizer', 'seed', 'pool', 'camping',
        'cooler', 'bug spray', 'insect', 'weed', 'landscape'
    ],
    'Groceries': [
        'milk', 'bread', 'banana', 'bananas', 'apple', 'apples', 'orange', 'oranges', 'berry',
        'berries', 'chicken', 'beef', 'pork', 'fish', 'salmon', 'shrimp', 'cheese', 'yogurt',
        'egg', 'eggs', 'butter', 'cream', 'lettuce', 'tomato', 'potato', 'rice', 'pasta',
        'bean', 'beans',
        'pizza', 'cereal', 'chips', 'cracker', 'cookies', 'snack', 'granola', 'juice', 'water',
        'coffee', 'tea', 'soup', 'broth', 'frozen', 'fruit', 'vegetable', 'veggie', 'meat',
        'tim hortons',
        'bakery', 'muffin', 'croissant', 'bagel', 'bagels', 'wrap', 'tortilla', 'pita',
        'salsa', 'sauce', 'olive oil', 'oil', 'vinegar', 'nuts', 'almond', 'cashew', 'peanut',
        'candy', 'chocolate', 'pop', 'soda', 'sparkling', 'drink', 'beverage', 'watermelon',
        'straw', 'hydro'
    ]
}

FUZZY_THRESHOLD = 0.75
FUZZY_MIN_GAP = 0.08
AI_ENGINE = AICategoryEngine(AI_SUGGESTIONS_PATH)
AI_STATS = AI_ENGINE.stats
APP_STATS = {'verified_hits': 0}
SKU_OVERRIDES = {
    '1898026': {
        'item': 'PREMIER PROTEIN VANILLA HIGH PROTEIN SHAKES',
        'category': 'Health & Fitness',
    },
    '1898053': {
        'item': 'PREMIER PROTEIN PEANUT BUTTER HIGH PROTEIN SHAKES',
        'category': 'Health & Fitness',
    },
    '265714': {
        'item': 'AO 8.8 CONTACT LENSES',
        'category': 'Medical Products',
    },
    '265711': {
        'item': 'AO 8.8 CONTACT LENSES',
        'category': 'Medical Products',
    },
    '676780': {
        'item': 'RENU CONTACT LENS SOLUTION 2X480ML',
        'category': 'Medical Products',
    },
    '6262016': {
        'item': 'KS BATH TISSUE',
        'category': 'Indoor Supplies',
    },
    '2005492': {
        'item': 'SV DIGITAL 1.67 LENSES',
        'category': 'Medical Products',
    },
    '1803338': {
        'item': 'HC6224U OPTICAL FRAME',
        'category': 'Medical Products',
    },
    '313740': {
        'item': 'KS FACIAL TISSUE',
        'category': 'Indoor Supplies',
    },
    '1828317': {
        'item': 'HERO MIGHTY PATCH',
        'category': 'Medical Products',
    },
    '1820534': {
        'item': 'ROOTS SWIMWEAR',
        'category': 'Clothing',
    },
    '128888': {
        'item': 'VECTOR JUMBO CEREAL',
        'category': 'Groceries',
    },
    '891': {
        'item': 'HARVEST CRUNCH CEREAL',
        'category': 'Groceries',
    },
}

# User-verified item classifications, embedded as deterministic policy.
VERIFIED_CATEGORY_OVERRIDES = {
    '16 grain': 'Groceries',
    '4oz choc muf': 'Groceries',
    'alani nu': 'Health & Fitness',
    'artisan bgt': 'Groceries',
    'asiancashw2p': 'Groceries',
    'avocados': 'Groceries',
    'b s breasts': 'Groceries',
    'bick s dills': 'Groceries',
    'boursin': 'Groceries',
    'brioche bun': 'Groceries',
    'broccoli': 'Groceries',
    'built sour': 'Groceries',
    'canned chckn': 'Groceries',
    'carrot 510g': 'Groceries',
    'cauliflower': 'Groceries',
    'cdn lt rye': 'Groceries',
    'cedar valley': 'Groceries',
    'chk bites': 'Groceries',
    'chkn pot pie': 'Groceries',
    'chow mein': 'Groceries',
    'cinnamon dan': 'Groceries',
    'connie ckn b': 'Groceries',
    'crmydillpckl': 'Groceries',
    'croutons': 'Groceries',
    'drive thru': 'Indoor Supplies',
    'gf ckn flngs': 'Groceries',
    'gogo squeez': 'Groceries',
    'green grapes': 'Groceries',
    'green kiwi': 'Groceries',
    'ground ckn': 'Groceries',
    'haddock': 'Groceries',
    'hv ranch': 'Groceries',
    'kinder ghssl': 'Groceries',
    'kodiak cakes': 'Groceries',
    'ks adult gum': 'Health & Fitness',
    'ks boneless': 'Groceries',
    'ks chia': 'Groceries',
    'ks chk tend': 'Groceries',
    'ks drawstrng': 'Clothing',
    'ks full zip': 'Clothing',
    'ks grk ygrt': 'Groceries',
    'lac free 2': 'Groceries',
    'love corn': 'Groceries',
    'med ch slice': 'Groceries',
    'michelob': 'Groceries',
    'mini cukes': 'Groceries',
    'mini wontons': 'Groceries',
    'nonni raspbr': 'Groceries',
    'org mangos': 'Groceries',
    'oroweat tort': 'Groceries',
    'philly choc': 'Groceries',
    'quepasa lime': 'Groceries',
    'quiche vty': 'Groceries',
    'remedy': 'Medical Products',
    'romaine': 'Groceries',
    'slcd bck bcn': 'Groceries',
    'sliced mango': 'Groceries',
    'smkd gouda': 'Groceries',
    'spn feta ssg': 'Groceries',
    'stuff pepper': 'Groceries',
    'sunions': 'Groceries',
    'sweet fries': 'Groceries',
    'sweet pepper': 'Groceries',
    'sweetkaleduo': 'Groceries',
    'terra dates': 'Groceries',
    'tonkotsu ram': 'Groceries',
    'tropicana og': 'Groceries',
    'twigz pickle': 'Groceries',
    'unsalted btr': 'Groceries',
    'vaseline dsr': 'Medical Products',
    'vector jumbo': 'Groceries',
    'hrvst crunch': 'Groceries',
}


def normalize_line(line: str) -> str:
    return re.sub(r'\s+', ' ', line).strip()


def normalize_for_match(text: str) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', text.lower()).strip()


def phrase_ngrams(tokens: List[str], n: int) -> List[str]:
    if len(tokens) < n:
        return []
    return [' '.join(tokens[i:i + n]) for i in range(len(tokens) - n + 1)]


def try_exact_keyword_category(desc: str) -> Optional[str]:
    d = desc.lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        for keyword in keywords:
            if keyword in d:
                return category
    return None


def try_fuzzy_keyword_category(desc: str) -> Optional[str]:
    normalized = normalize_for_match(desc)
    if not normalized:
        return None

    tokens = normalized.split()
    candidates = set(tokens)
    candidates.update(phrase_ngrams(tokens, 2))
    candidates.update(phrase_ngrams(tokens, 3))
    candidates.add(normalized)

    best_per_category: Dict[str, float] = {c: 0.0 for c in CATEGORY_KEYWORDS}

    for category, keywords in CATEGORY_KEYWORDS.items():
        for keyword in keywords:
            kw = normalize_for_match(keyword)
            kw_tokens = kw.split()
            kw_prefixes = {t[:3] for t in kw_tokens if len(t) >= 3}
            for cand in candidates:
                cand_tokens = cand.split()
                cand_prefixes = {t[:3] for t in cand_tokens if len(t) >= 3}

                # Guard 1: for single-word comparisons, enforce same starting letter.
                if (
                    len(cand_tokens) == 1
                    and len(kw_tokens) == 1
                    and cand
                    and kw
                    and cand[0] != kw[0]
                ):
                    continue

                score = difflib.SequenceMatcher(None, cand, kw).ratio()

                # Guard 2: penalize weak lexical affinity to reduce accidental matches
                # (e.g., brand names matching unrelated clothing keywords).
                has_affinity = (
                    bool(cand_prefixes & kw_prefixes)
                    or any(t in kw_tokens for t in cand_tokens)
                    or cand in kw
                    or kw in cand
                )
                if not has_affinity:
                    score -= 0.18

                if score > best_per_category[category]:
                    best_per_category[category] = score

    ranked = sorted(best_per_category.items(), key=lambda x: x[1], reverse=True)
    best_category, best_score = ranked[0]
    second_best_score = ranked[1][1] if len(ranked) > 1 else 0.0

    if best_score >= FUZZY_THRESHOLD and (best_score - second_best_score) >= FUZZY_MIN_GAP:
        return best_category
    return None


def chunk_list(items: List[Dict[str, str]], chunk_size: int) -> List[List[Dict[str, str]]]:
    if chunk_size <= 0:
        return [items] if items else []
    return [items[i:i + chunk_size] for i in range(0, len(items), chunk_size)]


def try_ai_category_batch(items: List[Dict[str, str]]) -> Dict[str, Optional[str]]:
    return AI_ENGINE.try_category_batch(items, source_label='Costco item')


def try_ai_category(desc: str, sku: Optional[str] = None) -> Optional[str]:
    return AI_ENGINE.try_single_category(desc, sku=sku, source_label='Costco item')


def parse_amount_token(token: str) -> float:
    token = token.replace('$', '')
    if token.endswith('-') and not token.startswith('-'):
        token = f'-{token[:-1]}'
    return float(token)


def extract_zip(zip_path: Path, out_dir: Path) -> List[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, 'r') as zf:
        zf.extractall(out_dir)
    return sorted(p for p in out_dir.rglob('*.pdf') if '__MACOSX' not in str(p))


def collect_input_pdfs(input_path: Path, extract_dir: Path) -> List[Path]:
    if not input_path.exists():
        raise FileNotFoundError(f'Missing input path: {input_path}')
    if input_path.is_file():
        if input_path.suffix.lower() == '.zip':
            return extract_zip(input_path, extract_dir)
        if input_path.suffix.lower() == '.pdf':
            return [input_path]
        raise RuntimeError(f'Unsupported input file type: {input_path}')
    if input_path.is_dir():
        return sorted(p for p in input_path.rglob('*.pdf'))
    raise RuntimeError(f'Unsupported input path: {input_path}')


def pdf_text(pdf_path: Path) -> str:
    text_parts: List[str] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            text_parts.append(page.extract_text() or '')
    return '\n'.join(text_parts)


def parse_receipt_amounts(text: str) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    subtotal = None
    total = None
    tax_total_line = None
    tax_components = 0.0
    has_tax_components = False

    for raw in text.splitlines():
        line = normalize_line(raw)

        # Capture grand total lines while excluding "TOTAL TAX" and "TOTAL NUMBER ..."
        is_total_label = (
            re.search(r'^\*+\s*TOTAL\b', line, re.I)
            or re.search(r'^\s*TOTAL\b(?!\s*(?:TAX|NUMBER)\b)', line, re.I)
        )
        if is_total_label:
            m_total = re.search(r'(-?\d+\.\d{2})$', line)
            if m_total:
                total = float(m_total.group(1))
                continue

        m_sub = re.search(r'\bSUBTOTAL\b.*?(-?\d+\.\d{2})$', line, re.I)
        if m_sub:
            subtotal = float(m_sub.group(1))
            continue

        m_total_tax = re.search(r'^\s*TOTAL\s+TAX\b.*?(-?\d+\.\d{2})$', line, re.I)
        if m_total_tax:
            tax_total_line = float(m_total_tax.group(1))
            continue

        m_tax = re.search(r'^\s*TAX\b.*?(-?\d+\.\d{2})$', line, re.I)
        if m_tax:
            tax_total_line = float(m_tax.group(1))
            continue

        m_tax_component = re.search(r'\b(?:GST|PST|HST)\b.*?(-?\d+\.\d{2})$', line, re.I)
        if m_tax_component:
            tax_components += float(m_tax_component.group(1))
            has_tax_components = True

    tax = tax_total_line if tax_total_line is not None else (round(tax_components, 2) if has_tax_components else None)
    return subtotal, tax, total


def parse_costco_order_id(text: str) -> Optional[str]:
    if not text:
        return None

    def normalize_candidate_digits(raw_digits: str) -> Optional[str]:
        digits = re.sub(r'\D', '', raw_digits or '')
        if not digits:
            return None
        # Costco receipt order ids observed in this dataset are 23 digits starting with "22".
        if len(digits) == 23 and digits.startswith('22'):
            return digits
        idx = digits.find('22')
        if idx != -1 and len(digits) >= idx + 23:
            cand = digits[idx:idx + 23]
            if cand.startswith('22'):
                return cand
        if 20 <= len(digits) <= 30:
            return digits
        return None

    lines = [normalize_line(ln) for ln in text.splitlines() if normalize_line(ln)]

    # Strong signal: order id near barcode label.
    for i, line in enumerate(lines):
        if re.search(r'(?i)\bbarcode\b', line):
            window = ' '.join(lines[i:i + 4])
            for m in LONG_NUM_FUZZY_RE.finditer(window):
                normalized = normalize_candidate_digits(m.group(1))
                if normalized:
                    return normalized

    patterns = [
        r'(?i)\border\s*(?:number|no\.?|#)?\b[^\d]{0,20}(\d{14,30})',
        r'(?i)\bonline\s*order\b[^\d]{0,20}(\d{14,30})',
        r'(?i)\border\s*id\b[^\d]{0,20}(\d{14,30})',
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            normalized = normalize_candidate_digits(m.group(1))
            if normalized:
                return normalized

    # Fallback: very long numeric token (e.g., Costco online order id).
    m2 = LONG_NUM_RE.search(text)
    if m2:
        normalized = normalize_candidate_digits(m2.group(1))
        if normalized:
            return normalized

    # Last resort: long numeric token with spaces/hyphens (common in OCR/text extraction).
    best: Optional[str] = None
    for m3 in LONG_NUM_FUZZY_RE.finditer(text):
        normalized = normalize_candidate_digits(m3.group(1))
        if normalized:
            if best is None:
                best = normalized
            elif len(normalized) > len(best):
                best = normalized
            elif normalized.startswith('22') and not best.startswith('22'):
                best = normalized
    if best:
        return best
    return None


def likely_item_line(line: str) -> bool:
    if not line:
        return False
    if SKIP_RE.search(line):
        return False
    if not AMOUNT_RE.search(line):
        return False
    if not re.search(r'[A-Za-z]', line) and not re.match(r'^\d{3,8}\b', line):
        return False
    return True


def classify_category_with_source(
    desc: str,
    sku: Optional[str] = None,
    allow_ai: bool = True,
) -> Tuple[str, str]:
    runtime_default = DEFAULT_CATEGORY if DEFAULT_CATEGORY in CATEGORY_ORDER else (CATEGORY_ORDER[0] if CATEGORY_ORDER else DEFAULT_CATEGORY)
    deterministic, source = deterministic_category(desc, source='costco', merchant='Costco')
    if deterministic not in CATEGORY_ORDER:
        deterministic = runtime_default
        source = 'default'
    if source == 'verified_cache':
        APP_STATS['verified_hits'] += 1
    if source != 'default':
        return deterministic, source

    if allow_ai:
        ai_category = try_ai_category(desc, sku=sku)
        if ai_category:
            return ai_category, 'ai'

    return deterministic, 'default'


def classify_category(desc: str) -> str:
    return classify_category_with_source(desc)[0]


def build_costco_search_url(sku: Optional[str], desc: str) -> str:
    query = sku if sku else desc
    return f'https://sameday.costco.ca/store/costco-canada/s?k={quote_plus(query)}'


def parse_item_line(line: str) -> Optional[Tuple[Optional[str], str, float]]:
    line = normalize_line(line)
    m = AMOUNT_RE.search(line)
    if not m:
        return None

    amount = parse_amount_token(m.group(1))
    desc = line[:m.start()].strip(' -')
    if not desc:
        return None

    # Some lines include an inline promo fragment before the final line amount,
    # e.g. "AO 8.8 -2.50 109.99 N". Keep the final amount and clean the item label.
    desc = normalize_line(re.sub(r'\s-\d+\.\d{2}\b', '', desc))

    sku = None
    # Costco lines are column-aligned: a leading numeric token is usually the item/SKU code.
    leading_col = re.match(r'^(\d{2,8})\s+(.+)$', desc)
    if leading_col:
        leading_sku = leading_col.group(1)
        leading_desc = normalize_line(leading_col.group(2))
        if leading_desc and re.search(r'[A-Za-z]', leading_desc):
            return leading_sku, leading_desc, amount

    sku_match = SKU_RE.search(desc)
    if sku_match:
        candidate_sku = sku_match.group(1)
        candidate_desc = normalize_line(desc.replace(candidate_sku, ''))
        if candidate_desc:
            sku = candidate_sku
            desc = candidate_desc

    return sku, desc, amount


def is_fragment_line(line: str) -> bool:
    line = normalize_line(line)
    if not line:
        return False
    if SKIP_RE.search(line):
        return False
    if AMOUNT_RE.search(line):
        return False
    if re.search(r'\d', line):
        return False
    if 'http://' in line.lower() or 'https://' in line.lower():
        return False
    return bool(re.search(r'[A-Za-z]', line))


def parse_receipt_items(file_stem: str, text: str) -> List[Dict]:
    rows: List[Dict] = []
    pending_ai_keys: Dict[str, List[int]] = {}
    pending_ai_items: Dict[str, Dict[str, str]] = {}
    raw_lines = text.splitlines()
    lines = [normalize_line(raw) for raw in raw_lines]
    last_item_idx: Optional[int] = None

    for pos, line in enumerate(lines):
        idx = pos + 1
        if not likely_item_line(line):
            continue

        parsed = parse_item_line(line)
        if not parsed:
            continue

        sku, desc, amount = parsed

        # Some receipts split descriptions across lines around the numeric SKU+amount row,
        # e.g. "GREEN" / "47825 12.99 N" / "GRAPES".
        if re.fullmatch(r'\d{2,8}', desc):
            prev_line = lines[pos - 1] if pos > 0 else ''
            next_line = lines[pos + 1] if pos + 1 < len(lines) else ''
            fragments = []
            if is_fragment_line(prev_line):
                fragments.append(prev_line)
            if is_fragment_line(next_line):
                fragments.append(next_line)
            if fragments:
                sku = sku or desc
                desc = normalize_line(' '.join(fragments))

        is_discount_line = amount < 0 and bool(re.search(r'\bTPD\b|TPD/', desc, re.I))
        if is_discount_line and last_item_idx is not None:
            rows[last_item_idx]['amount_cad'] = round(rows[last_item_idx]['amount_cad'] + amount, 2)
            rows[last_item_idx]['raw_line'] = f"{rows[last_item_idx]['raw_line']} | {line}"
            continue

        override = SKU_OVERRIDES.get(sku) if sku else None
        if override and override.get('item'):
            desc = override['item']

        if override and override.get('category'):
            category = override['category']
            category_source = 'override'
        else:
            category, category_source = classify_category_with_source(desc, sku=sku, allow_ai=False)
        url = build_costco_search_url(sku, desc)

        rows.append({
            'receipt_id': file_stem,
            'line_no': idx,
            'sku': sku,
            'item': desc,
            'amount_cad': amount,
            'budget_category': category,
            'category_source': category_source,
            'product_url': url,
            'raw_line': line,
        })
        last_item_idx = len(rows) - 1

        if category_source == 'default':
            cache_key = normalize_for_match(desc)
            cached = AI_ENGINE.get_cached_category_by_key(cache_key)
            if cached:
                rows[last_item_idx]['budget_category'] = cached
                rows[last_item_idx]['category_source'] = 'ai_cache'
            else:
                pending_ai_keys.setdefault(cache_key, []).append(last_item_idx)
                if cache_key not in pending_ai_items:
                    pending_ai_items[cache_key] = {
                        'id': cache_key,
                        'sku': sku or '',
                        'desc': desc,
                    }

    if pending_ai_items:
        batch_results: Dict[str, Optional[str]] = {}
        for batch in chunk_list(list(pending_ai_items.values()), AI_ENGINE.batch_size):
            batch_results.update(try_ai_category_batch(batch))
        for cache_key, row_idxs in pending_ai_keys.items():
            ai_category = batch_results.get(cache_key)
            if ai_category in CATEGORY_ORDER:
                AI_ENGINE.set_cached_category_by_key(cache_key, ai_category)
                for row_idx in row_idxs:
                    rows[row_idx]['budget_category'] = ai_category
                    rows[row_idx]['category_source'] = 'ai'
            else:
                AI_ENGINE.set_cached_category_by_key(cache_key, None)
    return rows


def autosize(ws) -> None:
    for col_cells in ws.columns:
        max_len = 0
        col_letter = col_cells[0].column_letter
        for cell in col_cells:
            val = '' if cell.value is None else str(cell.value)
            max_len = max(max_len, len(val))
        ws.column_dimensions[col_letter].width = min(max_len + 2, 50)


def style_header(ws, row: int = 1) -> None:
    fill = PatternFill('solid', fgColor='1F4E78')
    white_bold = Font(color='FFFFFF', bold=True)
    thin = Side(style='thin', color='D9E2F3')
    for cell in ws[row]:
        cell.fill = fill
        cell.font = white_bold
        cell.border = Border(bottom=thin)
        cell.alignment = Alignment(horizontal='center')


def add_items_sheet(wb: Workbook, item_rows: List[Dict]) -> None:
    ws = wb.active
    ws.title = 'Items'
    headers = ['receipt_id', 'line_no', 'sku', 'item', 'amount_cad', 'budget_category', 'category_source', 'product_url', 'raw_line']
    ws.append(headers)

    for row_idx, row in enumerate(item_rows, start=2):
        ws.cell(row=row_idx, column=1, value=row['receipt_id'])
        ws.cell(row=row_idx, column=2, value=row['line_no'])
        ws.cell(row=row_idx, column=3, value=row['sku'])
        ws.cell(row=row_idx, column=4, value=row['item'])
        ws.cell(row=row_idx, column=5, value=row['amount_cad'])
        ws.cell(row=row_idx, column=6, value=row['budget_category'])
        ws.cell(row=row_idx, column=7, value=row.get('category_source', ''))
        link_cell = ws.cell(row=row_idx, column=8, value='Costco Search')
        link_cell.hyperlink = row['product_url']
        link_cell.style = 'Hyperlink'
        ws.cell(row=row_idx, column=9, value=row['raw_line'])

    style_header(ws)
    autosize(ws)


def add_receipts_sheet(wb: Workbook, receipt_rows: List[Dict]) -> None:
    ws = wb.create_sheet('Receipts')
    headers = ['receipt_id', 'order_id', 'subtotal_cad', 'tax_cad', 'total_cad', 'parsed_item_count']
    ws.append(headers)
    for row in receipt_rows:
        ws.append([row[h] for h in headers])
    style_header(ws)
    autosize(ws)


def add_category_summary_sheet(wb: Workbook, item_rows: List[Dict]) -> None:
    ws = wb.create_sheet('Receipt Category Totals')
    categories = list(CATEGORY_ORDER)
    if not categories:
        categories = [DEFAULT_CATEGORY]
    headers = ['receipt_id'] + categories + ['Total']
    ws.append(headers)

    receipt_ids = sorted({row['receipt_id'] for row in item_rows})
    for row_idx, receipt_id in enumerate(receipt_ids, start=2):
        ws.cell(row=row_idx, column=1, value=receipt_id)
        for col_idx in range(2, 2 + len(categories)):
            category_header_cell = ws.cell(row=1, column=col_idx).coordinate
            ws.cell(
                row=row_idx,
                column=col_idx,
                value=f'=SUMIFS(Items!$E:$E,Items!$A:$A,$A{row_idx},Items!$F:$F,{category_header_cell})'
            )
        ws.cell(
            row=row_idx,
            column=2 + len(categories),
            value=f'=SUM(B{row_idx}:{ws.cell(row=1, column=1 + len(categories)).column_letter}{row_idx})'
        )

    style_header(ws)
    autosize(ws)


def add_notes_sheet(wb: Workbook, notes: List[str]) -> None:
    ws = wb.create_sheet('Notes')
    ws.append(['note'])
    for note in notes:
        ws.append([note])
    style_header(ws)
    autosize(ws)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Extract Costco receipt data and optionally write canonical DB records.')
    parser.add_argument('--zip-path', default=str(ZIP_PATH), help='Input path: ZIP file, single PDF, or directory of PDFs.')
    parser.add_argument('--extract-dir', default=str(EXTRACT_DIR), help='Directory to extract PDFs to.')
    parser.add_argument('--out-xlsx', default=str(OUT_XLSX), help='Output Excel workbook path.')
    parser.add_argument('--write-db', action='store_true', help='Write parsed canonical expense records into Postgres.')
    parser.add_argument(
        '--db-dsn',
        default=os.environ.get('HOME_BUDGET_PG_DSN', ''),
        help=(
            'Postgres DSN for --write-db. Do not include password here. '
            'Use ~/.pgpass or PGPASSWORD/HOME_BUDGET_PGPASSWORD env vars. '
            'Default: env HOME_BUDGET_PG_DSN or dbname=home_budget.'
        ),
    )
    parser.add_argument('--db-schema', default='budget', help='Target schema for --write-db (default: budget).')
    return parser.parse_args()


def _require_valid_schema_name(schema: str) -> str:
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', schema or ''):
        raise ValueError(f'Invalid --db-schema value: {schema!r}')
    return schema


def _dsn_contains_password(dsn: str) -> bool:
    if not dsn:
        return False
    key_value_password = re.search(r'(^|\s)password\s*=', dsn, re.IGNORECASE)
    uri_password = re.search(r'://[^/\s:@]+:[^@\s/]*@', dsn)
    return bool(key_value_password or uri_password)


def _resolve_postgres_dsn(raw_dsn: str) -> str:
    dsn = (raw_dsn or '').strip()
    if _dsn_contains_password(dsn):
        raise ValueError(
            'Unsafe --db-dsn: inline password detected. '
            'Use ~/.pgpass (recommended) or PGPASSWORD/HOME_BUDGET_PGPASSWORD env vars.'
        )
    return dsn or 'dbname=home_budget'


def _connect_postgres(dsn: str):
    if (dsn or '').strip():
        dsn = _resolve_postgres_dsn(dsn)
    else:
        # Kubernetes supplies the application connection as a secret-backed
        # environment variable. It is safe to consume here without echoing it
        # or requiring it to be repeated on the command line.
        dsn = os.environ.get('DATABASE_URL', '').strip() or 'dbname=home_budget'
    hb_pg_password = os.environ.get('HOME_BUDGET_PGPASSWORD', '').strip()
    if hb_pg_password and not os.environ.get('PGPASSWORD'):
        os.environ['PGPASSWORD'] = hb_pg_password
    try:
        import psycopg  # type: ignore

        return psycopg.connect(dsn)
    except Exception:
        try:
            import psycopg2  # type: ignore

            return psycopg2.connect(dsn)
        except Exception as e:
            raise RuntimeError(
                'Could not connect to Postgres. Install psycopg (or psycopg2-binary) and verify --db-dsn. '
                'For auth, prefer ~/.pgpass or libpq env vars.'
            ) from e


def to_money_decimal(value: object, default_zero: bool = False) -> Optional[Decimal]:
    if value is None:
        return Decimal('0.00') if default_zero else None
    try:
        return Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return Decimal('0.00') if default_zero else None


def canonical_category_source(value: object) -> str:
    """Map extractor detail labels onto the canonical audit vocabulary."""
    source = str(value or '').strip().lower()
    if source == 'ai':
        return 'ai'
    if source in {'default', ''}:
        return 'unresolved'
    return 'rule'


def load_expense_categories_from_postgres(dsn: str, schema: str) -> List[str]:
    schema = _require_valid_schema_name(schema)
    conn = _connect_postgres(dsn)
    sql = f"""
        SELECT category_name
        FROM {schema}.expense_categories
        WHERE is_active
        ORDER BY id
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
        return [str(r[0]).strip() for r in rows if r and str(r[0]).strip()]
    finally:
        conn.close()


def load_expense_category_mappings_from_postgres(dsn: str, schema: str) -> List[Dict[str, object]]:
    schema = _require_valid_schema_name(schema)
    conn = _connect_postgres(dsn)
    sql = f"""
        SELECT source, merchant, match_type, match_text, category_name, priority
        FROM {schema}.expense_category_mappings
        WHERE is_active
        ORDER BY priority, id
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
        return [
            {
                'source': r[0],
                'merchant': r[1],
                'match_type': r[2],
                'match_text': r[3],
                'category_name': r[4],
                'priority': r[5],
            }
            for r in rows
            if r and r[3] and r[4]
        ]
    finally:
        conn.close()


def _receipt_date_from_id(receipt_id: str) -> Optional[datetime.date]:
    m = DATE_NAME_RE.match(f'{receipt_id}.pdf')
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1)[:8], '%Y%m%d').date()
    except ValueError:
        return None


def write_costco_to_postgres(
    receipt_rows: List[Dict],
    all_items: List[Dict],
    dsn: str,
    schema: str,
) -> Tuple[int, int]:
    schema = _require_valid_schema_name(schema)
    conn = _connect_postgres(dsn)
    written_expenses = 0
    written_items = 0

    items_by_receipt: Dict[str, List[Dict]] = {}
    for item in all_items:
        items_by_receipt.setdefault(item['receipt_id'], []).append(item)

    expense_sql = f"""
        INSERT INTO {schema}.expenses (
            source, order_id, receipt_filename, order_date, store_name, order_url,
            receipt_item_subtotal, receipt_total_charged, expense_total, raw_payload
        ) VALUES (
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s::jsonb
        )
        ON CONFLICT (source, order_id)
        DO UPDATE SET
            receipt_filename = EXCLUDED.receipt_filename,
            order_date = EXCLUDED.order_date,
            store_name = EXCLUDED.store_name,
            order_url = EXCLUDED.order_url,
            receipt_item_subtotal = EXCLUDED.receipt_item_subtotal,
            receipt_total_charged = EXCLUDED.receipt_total_charged,
            expense_total = EXCLUDED.expense_total,
            raw_payload = EXCLUDED.raw_payload
        RETURNING id
    """
    delete_items_sql = f'DELETE FROM {schema}.expense_items WHERE expense_pk = %s'
    item_sql = f"""
        INSERT INTO {schema}.expense_items (
            expense_pk, item_name, budget_category, category_source,
            unit_qty, unit_cost, line_total
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
    """

    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            for receipt in receipt_rows:
                receipt_id = receipt['receipt_id']
                order_id = str(receipt.get('order_id') or receipt_id)
                receipt_items = items_by_receipt.get(receipt_id, [])
                subtotal = receipt.get('subtotal_cad')
                total = receipt.get('total_cad')
                subtotal_dec = to_money_decimal(subtotal)
                total_dec = to_money_decimal(total)
                if total_dec is None and subtotal_dec is not None and receipt.get('tax_cad') is not None:
                    tax_dec = to_money_decimal(receipt.get('tax_cad'))
                    if tax_dec is not None:
                        total_dec = (subtotal_dec + tax_dec).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                payload = {
                    'receipt': receipt,
                    'items': receipt_items,
                }
                cur.execute(
                    expense_sql,
                    (
                        'costco',
                        order_id,
                        f'{receipt_id}.pdf',
                        _receipt_date_from_id(receipt_id),
                        'Costco',
                        None,
                        subtotal_dec,
                        total_dec,
                        total_dec,
                        json.dumps(payload, ensure_ascii=True),
                    ),
                )
                row = cur.fetchone()
                if row is None:
                    continue
                expense_pk = int(row[0])
                written_expenses += 1

                cur.execute(delete_items_sql, (expense_pk,))
                # Collapse exact duplicate lines within one receipt into a single canonical row.
                # This avoids unique-index collisions and preserves spend by summing qty/line total.
                grouped_items: Dict[Tuple[str, Optional[str], Optional[str], Optional[float]], Dict[str, Optional[float]]] = {}
                for item in receipt_items:
                    raw_name = str(item.get('item') or '').strip()
                    item_name = re.sub(r'\s+', ' ', raw_name)
                    unit_cost = item.get('amount_cad')
                    try:
                        unit_cost_val = float(unit_cost) if unit_cost is not None else None
                    except Exception:
                        unit_cost_val = None
                    key = (
                        item_name.lower(),
                        item.get('budget_category'),
                        canonical_category_source(item.get('category_source')),
                        unit_cost_val,
                    )
                    slot = grouped_items.get(key)
                    if slot is None:
                        grouped_items[key] = {
                            'item_name': item_name,
                            'budget_category': item.get('budget_category'),
                            'category_source': canonical_category_source(item.get('category_source')),
                            'unit_qty': 1.0,
                            'unit_cost': unit_cost_val,
                            'line_total': unit_cost_val,
                        }
                    else:
                        slot['unit_qty'] = float(slot.get('unit_qty') or 0.0) + 1.0
                        if unit_cost_val is not None:
                            slot['line_total'] = float(slot.get('line_total') or 0.0) + unit_cost_val

                for merged in grouped_items.values():
                    cur.execute(
                        item_sql,
                        (
                            expense_pk,
                            merged['item_name'],
                            merged['budget_category'],
                            merged['category_source'],
                            merged['unit_qty'],
                            to_money_decimal(merged['unit_cost']),
                            to_money_decimal(merged['line_total']),
                        ),
                    )
                    written_items += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return written_expenses, written_items


def main() -> None:
    args = parse_args()
    input_path = Path(args.zip_path).expanduser().resolve()
    extract_dir = Path(args.extract_dir).expanduser().resolve()
    out_xlsx = Path(args.out_xlsx).expanduser().resolve()

    try:
        db_categories = load_expense_categories_from_postgres(args.db_dsn, args.db_schema)
        if db_categories:
            CATEGORY_ORDER[:] = db_categories
            print(f'Loaded categories from {args.db_schema}.expense_categories: {len(db_categories)} active', flush=True)
        else:
            print('Warning: no active rows in expense_categories; using built-in categories.', flush=True)
    except Exception as e:
        print(f'Warning: could not load categories from DB; using built-in categories. {type(e).__name__}: {e}', flush=True)
    try:
        mapping_rules = load_expense_category_mappings_from_postgres(args.db_dsn, args.db_schema)
        set_runtime_category_rules(mapping_rules)
        print(f'Loaded category mapping rules: {len(mapping_rules)} active', flush=True)
    except Exception as e:
        set_runtime_category_rules([])
        print(f'Warning: could not load category mapping rules; continuing without them. {type(e).__name__}: {e}', flush=True)

    run_started = time.perf_counter()
    extract_seconds = 0.0
    pdf_read_seconds = 0.0
    parse_seconds = 0.0
    xlsx_write_seconds = 0.0
    suggestions_save_seconds = 0.0

    AI_ENGINE.load_suggestions()
    extract_started = time.perf_counter()
    pdfs = collect_input_pdfs(input_path, extract_dir)
    extract_seconds = time.perf_counter() - extract_started

    all_items: List[Dict] = []
    receipt_rows: List[Dict] = []
    notes: List[str] = [
        'Product links use Costco Canada same-day search URLs. They are search links, not guaranteed exact product pages.',
        (
            'Category assignment uses SKU override, then embedded verified overrides, '
            'then exact keyword match, then fuzzy keyword match, '
            'and AI fallback when OPENAI_API_KEY is set '
            '(disable with ENABLE_AI_CATEGORY_FALLBACK=0).'
        ),
        'Items.category_source shows which method assigned each category: '
        'override, verified_cache, exact, fuzzy, ai_cache, ai, or default.',
        (
            f'AI suggestions cache file: {AI_SUGGESTIONS_PATH.name}. '
            'It stores non-verified AI classifications for review before promoting '
            'to VERIFIED_CATEGORY_OVERRIDES.'
        ),
    ]

    for pdf in pdfs:
        if not DATE_NAME_RE.match(pdf.name):
            notes.append(f'Skipped unexpected filename format: {pdf.name}')
            continue

        stem = pdf.stem
        read_started = time.perf_counter()
        text = pdf_text(pdf)
        pdf_read_seconds += time.perf_counter() - read_started
        parse_started = time.perf_counter()
        items = parse_receipt_items(stem, text)
        subtotal, tax, total = parse_receipt_amounts(text)
        parsed_order_id = parse_costco_order_id(text) or stem
        parse_seconds += time.perf_counter() - parse_started

        all_items.extend(items)

        item_sum = round(sum(r['amount_cad'] for r in items), 2)
        receipt_rows.append({
            'receipt_id': stem,
            'order_id': parsed_order_id,
            'subtotal_cad': subtotal,
            'tax_cad': tax,
            'total_cad': total,
            'parsed_item_count': len(items),
            'parsed_item_sum_cad': item_sum,
        })

        compare_target = subtotal if subtotal is not None else total
        if compare_target is not None and abs(item_sum - compare_target) > 5:
            notes.append(
                f'{stem}: parsed item sum {item_sum:.2f} differs from '
                f'{"subtotal" if subtotal is not None else "total"} {compare_target:.2f}; '
                'some lines may be discounts, split lines, or non-item rows.'
            )

    notes.append(
        'AI Status: '
        f'enabled={AI_ENGINE.enabled}, '
        f'disabled_runtime={AI_ENGINE.runtime_disabled}, '
        f'max_calls_per_run={AI_ENGINE.max_calls_per_run}, '
        f'calls={AI_STATS["calls"]}, '
        f'batch_items={AI_STATS["batch_items"]}, '
        f'verified_hits={APP_STATS["verified_hits"]}, '
        f'ai_cache_hits={AI_STATS["ai_cache_hits"]}, '
        f'skipped_call_budget={AI_STATS["skipped_call_budget"]}, '
        f'success={AI_STATS["success"]}, '
        f'api_errors={AI_STATS["api_errors"]}, '
        f'no_result={AI_STATS["no_result"]}, '
        f'call_seconds_total={AI_STATS["call_seconds_total"]:.2f}, '
        f'call_seconds_avg={(AI_STATS["call_seconds_total"] / AI_STATS["calls"]) if AI_STATS["calls"] else 0.0:.2f}, '
        f'call_seconds_max={AI_STATS["call_seconds_max"]:.2f}.'
    )
    hist = AI_STATS['batch_histogram']
    hist_keys = sorted(hist, key=int)
    hist_text = ','.join(f'{k}:{hist[k]}' for k in hist_keys) if hist_keys else 'none'

    notes.append(
        'AI Latency Detail: '
        f'batch_size={AI_ENGINE.batch_size}, '
        f'call_items_max={AI_STATS["call_items_max"]}, '
        f'slow_calls(>={AI_ENGINE.slow_call_seconds:.1f}s)={AI_STATS["slow_calls"]}, '
        f'calls<=2s={AI_STATS["duration_le_2s"]}, '
        f'calls2-5s={AI_STATS["duration_2_to_5s"]}, '
        f'calls5-8s={AI_STATS["duration_5_to_8s"]}, '
        f'calls>8s={AI_STATS["duration_gt_8s"]}, '
        f'batch_histogram={hist_text}.'
    )
    notes.append(
        'Timing: '
        f'total={time.perf_counter() - run_started:.2f}s, '
        f'extract_zip={extract_seconds:.2f}s, '
        f'pdf_read={pdf_read_seconds:.2f}s, '
        f'parse_classify={parse_seconds:.2f}s.'
    )

    all_items = sorted(all_items, key=lambda x: (x['receipt_id'], x['line_no']))
    receipt_rows = sorted(receipt_rows, key=lambda x: x['receipt_id'])

    wb = Workbook()
    add_items_sheet(wb, all_items)
    add_receipts_sheet(wb, receipt_rows)
    add_category_summary_sheet(wb, all_items)
    add_notes_sheet(wb, notes)
    xlsx_started = time.perf_counter()
    wb.save(out_xlsx)
    xlsx_write_seconds = time.perf_counter() - xlsx_started
    suggestions_started = time.perf_counter()
    AI_ENGINE.save_suggestions()
    suggestions_save_seconds = time.perf_counter() - suggestions_started
    print(
        f'Runtime timing: total={time.perf_counter() - run_started:.2f}s, '
        f'extract_zip={extract_seconds:.2f}s, '
        f'pdf_read={pdf_read_seconds:.2f}s, '
        f'parse_classify={parse_seconds:.2f}s, '
        f'xlsx_write={xlsx_write_seconds:.2f}s, '
        f'suggestions_save={suggestions_save_seconds:.2f}s'
    )

    if args.write_db:
        written_expenses, written_items = write_costco_to_postgres(
            receipt_rows=receipt_rows,
            all_items=all_items,
            dsn=args.db_dsn,
            schema=args.db_schema,
        )
        print(
            f'Wrote Postgres: schema={args.db_schema}, expenses={written_expenses}, expense_items={written_items}'
        )

    print(f'Wrote workbook: {out_xlsx}')


if __name__ == '__main__':
    main()
