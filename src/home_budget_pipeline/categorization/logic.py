#!/usr/bin/env python3
"""Shared deterministic budget category logic used by receipt and transaction pipelines."""

from __future__ import annotations

import difflib
import re
from typing import Dict, List, Optional, Tuple

DEFAULT_CATEGORY = 'Groceries'
CATEGORY_ORDER = [
    'Child Support', 'Autopac', 'Car Payment', 'Car Repair', 'Car Replacement Fund',
    'Real Estate Tax', 'Rental Expenses', 'Cleaning', 'Clothing', 'Debt', 'Dining',
    'Misc Household', 'Medical ProfSvcs', 'University / Books', 'Emergency Fund',
    'Fuel', 'Fun / Entertainment', 'Furniture / Appliances', 'Birthday / Celebrations',
    'Groceries', 'IncomeTax Due', 'Home Insurance', 'Mortgage PrePayment',
    'Interest Expense', 'Life Insurance', 'Medical Products', 'LTD Insurance',
    'Home Mortgage', 'Lake', 'Sinking Fund', 'Donations', 'Vacation', 'Christmas',
    'Home Improvement', 'RRSP GIC', 'RRSP', 'Health & Fitness', 'Accounting', 'Hydro',
    'Online Svcs', 'Wireless', 'TV', 'Water', 'Bank Fee', 'MLCC', 'Cash/Unknown',
    'Kids Clothing', 'Kids Sports', 'Emp Reimburse', 'Principal Expense',
    'Student Expense', 'Hair/Salon/Body Care', 'Future Use1', 'Indoor Supplies',
    'Outdoor Supplies', 'Future Use2', 'Parking', 'Shareholder loan', 'TFSA',
    'Asset Purchase',
]
VALID_CATEGORIES = set(CATEGORY_ORDER)
FUZZY_THRESHOLD = 0.75
FUZZY_MIN_GAP = 0.08
RUNTIME_CATEGORY_RULES: List[Dict[str, object]] = []

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
        'protein shake', 'protein shakes', 'shake', 'powder', 'collagen', 'magnesium',
        'wakewater', 'built puff', 'quest', 'fairlife', 'premier protein'
    ],
    'Clothing': [
        'shirt', 'pant', 'pants', 'jeans', 'jacket', 'coat', 'hoodie', 'sock', 'socks',
        'underwear', 'brief', 'boxer', 'bra', 'shoe', 'shoes', 'boot', 'boots',
        'slipper', 'hat', 'glove', 'gloves', 'sweater', 'legging', 'leggings',
        'short', 'shorts', 'dress', 'pajama', 'tee', 't-shirt', 'parka', 'fleece',
        'crew', 'crewneck', 'polo', 'lounge', 'dkny', 'clothing', 'mondetta', 'vest',
        'top', 'adidas', 'tshirt', 'tee shirt', 'tshrit', 'thong', 'tankini',
        'swimwear', 'knix'
    ],
    'Indoor Supplies': [
        'paper towel', 'toilet paper', 'tissue', 'napkin', 'detergent', 'laundry',
        'dish soap', 'dishwasher', 'soap', 'cleaner', 'cleaning', 'garbage bag',
        'garbage bags', 'kitchen bag', 'ziplock', 'foil', 'plastic wrap', 'parchment',
        'sponge', 'sponges', 'batteries', 'battery', 'light bulb', 'lightbulb', 'bulb',
        'bulbs', 'kleenex', 'wipes', 'sanitizer', 'disinfect', 'trash bag', 'trash bags',
        'plate', 'plates', 'bounty', 'swiffer'
    ],
    'Outdoor Supplies': [
        'soil', 'mulch', 'garden', 'planter', 'hose', 'bbq', 'propane', 'charcoal',
        'patio', 'deck', 'outdoor', 'lawn', 'grass seed', 'fertilizer', 'seed', 'pool',
        'camping', 'cooler', 'bug spray', 'insect', 'weed', 'landscape'
    ],
    'Groceries': [
        'milk', 'bread', 'banana', 'bananas', 'apple', 'apples', 'orange', 'oranges',
        'berry', 'berries', 'chicken', 'beef', 'pork', 'fish', 'salmon', 'shrimp',
        'cheese', 'yogurt', 'egg', 'eggs', 'butter', 'cream', 'lettuce', 'tomato',
        'potato', 'rice', 'pasta', 'bean', 'beans', 'pizza', 'cereal', 'chips',
        'cracker', 'cookies', 'snack', 'granola', 'juice', 'water', 'coffee', 'tea',
        'soup', 'broth', 'frozen', 'fruit', 'vegetable', 'veggie', 'meat',
        'tim hortons', 'bakery', 'muffin', 'croissant', 'bagel', 'bagels', 'wrap',
        'tortilla', 'pita', 'salsa', 'sauce', 'olive oil', 'oil', 'vinegar', 'nuts',
        'almond', 'cashew', 'peanut', 'candy', 'chocolate', 'pop', 'soda', 'sparkling',
        'drink', 'beverage', 'watermelon', 'straw', 'hydro'
    ]
}

CATEGORY_ALIASES = {
    'online services': 'Online Svcs',
    'medical prof svcs': 'Medical ProfSvcs',
    'medical prof services': 'Medical ProfSvcs',
    'cash unknown': 'Cash/Unknown',
    'shareholder loan': 'Shareholder loan',
    'misc paul rox': 'Misc Household',
    'misc paul and rox': 'Misc Household',
}

VERIFIED_CATEGORY_OVERRIDES = {
    '16 grain': 'Groceries', '4oz choc muf': 'Groceries', 'alani nu': 'Health & Fitness',
    'artisan bgt': 'Groceries', 'asiancashw2p': 'Groceries', 'avocados': 'Groceries',
    'b s breasts': 'Groceries',
    'bausch lomb renu fresh twin pack multi purpose contact lens solution': 'Medical Products',
    'bick s dills': 'Groceries', 'boiron arnicare gel': 'Medical Products',
    'boursin': 'Groceries', 'brioche bun': 'Groceries', 'broccoli': 'Groceries',
    'built cookies n cream peanut butter cup puff protein bars': 'Health & Fitness',
    'built sour': 'Groceries', 'canned chckn': 'Groceries', 'carrot 510g': 'Groceries',
    'cauliflower': 'Groceries', 'cdn lt rye': 'Groceries', 'cedar valley': 'Groceries',
    'cerave moisturizing cream': 'Indoor Supplies',
    'cetaphil sensitive gentle skin cleanser': 'Indoor Supplies', 'chk bites': 'Groceries',
    'chkn pot pie': 'Groceries', 'chow mein': 'Groceries', 'cinnamon dan': 'Groceries',
    'connie ckn b': 'Groceries', 'crmydillpckl': 'Groceries', 'croutons': 'Groceries',
    'dawn platinum powerwash dish spray with refills': 'Indoor Supplies',
    'delon 100 cotton premium cosmetic rounds': 'Indoor Supplies', 'drive thru': 'Indoor Supplies',
    'downy april fresh ultra liquid fabric conditioner': 'Indoor Supplies',
    'dove daily moisture hydration conditioner': 'Indoor Supplies',
    'dove daily moisture shampoo': 'Indoor Supplies',
    'feit electric led string lights': 'Outdoor Supplies', 'gf ckn flngs': 'Groceries',
    'gogo squeez': 'Groceries', 'green grapes': 'Groceries',
    'green seedless grapes': 'Groceries', 'green kiwi': 'Groceries', 'ground ckn': 'Groceries',
    'haddock': 'Groceries', 'harvest brown n serve bread sticks': 'Groceries',
    'hero mighty patch original invisible patch pack': 'Medical Products', 'hv ranch': 'Groceries',
    'imodium quick dissolve diarrhea relief loperamide hydrochloride tablets': 'Medical Products',
    'kinder ghssl': 'Groceries', 'kodiak cakes': 'Groceries', 'ks adult gum': 'Health & Fitness',
    'ks boneless': 'Groceries', 'ks chia': 'Groceries', 'ks chk tend': 'Groceries',
    'ks drawstrng': 'Clothing', 'ks full zip': 'Clothing', 'ks grk ygrt': 'Groceries',
    'kirkland signature daily facial towelettes': 'Indoor Supplies',
    'kirkland signature organic chia seeds': 'Groceries', 'lac free 2': 'Groceries',
    'love corn': 'Groceries', 'med ch slice': 'Groceries', 'michelob': 'Groceries',
    'mini cukes': 'Groceries', 'mini wontons': 'Groceries', 'nonni raspbr': 'Groceries',
    'org mangos': 'Groceries', 'oroweat tort': 'Groceries', 'philly choc': 'Groceries',
    'quepasa lime': 'Groceries', 'quaker harvest crunch original granola cereal': 'Groceries',
    'quiche vty': 'Groceries', 'remedy': 'Medical Products', 'romaine': 'Groceries',
    'scott original shop towels': 'Outdoor Supplies', 'slcd bck bcn': 'Groceries',
    'sliced mango': 'Groceries', 'smkd gouda': 'Groceries',
    'snowcrest foods organic sliced strawberries': 'Groceries', 'spn feta ssg': 'Groceries',
    'stuff pepper': 'Groceries', 'sunions': 'Groceries', 'sweet fries': 'Groceries',
    'sweet pepper': 'Groceries', 'sweetkaleduo': 'Groceries', 'terra dates': 'Groceries',
    'tide high efficiency turbo powder laundry detergent with acti life': 'Indoor Supplies',
    'tonkotsu ram': 'Groceries', 'tropicana og': 'Groceries', 'twigz pickle': 'Groceries',
    'unsalted btr': 'Groceries', 'vaseline dry skin repair body lotion': 'Indoor Supplies',
    'vaseline dsr': 'Medical Products', 'vector jumbo': 'Groceries', 'hrvst crunch': 'Groceries',
    'wahl cordless pro home barber kit 1 each': 'Indoor Supplies',
    'x lite corporation utility lighters': 'Outdoor Supplies',
}


def normalize_for_match(text: str) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', text.lower()).strip()


def canonicalize_category(category: Optional[str]) -> Optional[str]:
    if not category:
        return None
    cleaned = category.strip()
    if cleaned in VALID_CATEGORIES:
        return cleaned
    lowered = normalize_for_match(cleaned)
    for candidate in CATEGORY_ORDER:
        if normalize_for_match(candidate) == lowered:
            return candidate
    return CATEGORY_ALIASES.get(lowered)


def set_runtime_category_rules(rules: List[Dict[str, object]]) -> None:
    normalized_rules: List[Dict[str, object]] = []
    for idx, raw in enumerate(rules):
        category_name = canonicalize_category(str(raw.get('category_name') or ''))
        if category_name not in VALID_CATEGORIES:
            continue
        match_type = str(raw.get('match_type') or 'contains').strip().lower()
        if match_type not in {'exact', 'contains'}:
            continue
        match_text_norm = normalize_for_match(str(raw.get('match_text') or ''))
        if not match_text_norm:
            continue
        source_norm = normalize_for_match(str(raw.get('source') or ''))
        merchant_norm = normalize_for_match(str(raw.get('merchant') or ''))
        priority_raw = raw.get('priority')
        try:
            priority = int(priority_raw) if priority_raw is not None else 100
        except Exception:
            priority = 100
        normalized_rules.append({
            'category_name': category_name, 'match_type': match_type,
            'match_text_norm': match_text_norm, 'source_norm': source_norm or None,
            'merchant_norm': merchant_norm or None, 'priority': priority, '_idx': idx,
        })
    normalized_rules.sort(key=lambda r: (int(r['priority']), int(r['_idx'])))
    RUNTIME_CATEGORY_RULES[:] = normalized_rules


def match_runtime_category_rule(desc: str, source: Optional[str] = None,
                                merchant: Optional[str] = None) -> Optional[str]:
    if not RUNTIME_CATEGORY_RULES:
        return None
    desc_norm = normalize_for_match(desc)
    if not desc_norm:
        return None
    source_norm = normalize_for_match(source or '') or None
    merchant_norm = normalize_for_match(merchant or '') or None
    for rule in RUNTIME_CATEGORY_RULES:
        if rule.get('source_norm') and rule.get('source_norm') != source_norm:
            continue
        if rule.get('merchant_norm') and rule.get('merchant_norm') != merchant_norm:
            continue
        match_text = str(rule['match_text_norm'])
        match_type = str(rule['match_type'])
        if match_type == 'exact' and desc_norm == match_text:
            return str(rule['category_name'])
        if match_type == 'contains' and match_text in desc_norm:
            return str(rule['category_name'])
    return None


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
                if len(cand_tokens) == 1 and len(kw_tokens) == 1 and cand and kw and cand[0] != kw[0]:
                    continue
                score = difflib.SequenceMatcher(None, cand, kw).ratio()
                has_affinity = (bool(cand_prefixes & kw_prefixes)
                                or any(t in kw_tokens for t in cand_tokens)
                                or cand in kw or kw in cand)
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


def deterministic_category(desc: str, source: Optional[str] = None,
                           merchant: Optional[str] = None) -> Tuple[str, str]:
    d = desc.lower()
    cache_key = normalize_for_match(desc)
    cached_category = VERIFIED_CATEGORY_OVERRIDES.get(cache_key)
    if cached_category in CATEGORY_ORDER:
        return cached_category, 'verified_cache'
    if 'protein bar' in d or 'protein bars' in d:
        return 'Groceries', 'exact'
    if 'enviro fee' in d:
        return 'Groceries', 'exact'
    if 'crest' in d:
        return 'Indoor Supplies', 'exact'
    mapped = match_runtime_category_rule(desc, source=source, merchant=merchant)
    if mapped in CATEGORY_ORDER:
        return mapped, 'mapping_rule'
    exact = try_exact_keyword_category(desc)
    if exact:
        return exact, 'exact'
    fuzzy = try_fuzzy_keyword_category(desc)
    if fuzzy:
        return fuzzy, 'fuzzy'
    return DEFAULT_CATEGORY, 'default'
