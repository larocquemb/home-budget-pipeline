"""Interpret retailer item codes while retaining the original receipt reading."""
import re
from decimal import Decimal, InvalidOperation


def item_identity(item, merchant, source_items=()):
    original = str(item.get('item_name') or '')
    result = {'product_code': None, 'receipt_item_name': original,
              'discount_for_product_code': None}
    # Costco warehouse item numbers are separate from quantities/package sizes.
    # This layout rule applies to all its receipts, never an individual SKU.
    if not re.search(r'\bcostco\b', merchant or '', re.I):
        return result
    match = re.fullmatch(r'\s*(\d{3,8})\s+(\S.*)', original)
    if match and not re.match(r'^(?:g|kg|mg|ml|l|oz|lb|lbs)\b', match[2], re.I):
        result.update(product_code=match[1], receipt_item_name=match[2])
    else:
        code = item.get('product_code') or item.get('sku')
        if isinstance(code, str) and re.fullmatch(r'\d{3,8}', code):
            result['product_code'] = code
    if not result['product_code']:
        codes = set()
        for source in source_items or ():
            if not isinstance(source, dict) or source.get('item') != original:
                continue
            try:
                if Decimal(str(source.get('amount_cad'))) != Decimal(str(item.get('line_total'))):
                    continue
            except InvalidOperation:
                continue
            code = str(source.get('sku') or '')
            if re.fullmatch(r'\d{3,8}', code):
                codes.add(code)
        if len(codes) == 1:
            result['product_code'] = codes.pop()
    discount = re.fullmatch(r'TPD\s*/\s*(\d{3,8})', result['receipt_item_name'], re.I)
    if discount:
        result['discount_for_product_code'] = discount[1]
    return result


def matching_item_name(item, merchant, source_items=()):
    identity = item_identity(item, merchant, source_items)
    if identity['product_code']:
        return identity['product_code'] + ' ' + identity['receipt_item_name']
    return str(item.get('item_name') or '')
