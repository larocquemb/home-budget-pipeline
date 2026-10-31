"""Cent-precise allocation checks, independent of OCR confidence."""
from decimal import Decimal, InvalidOperation


def money(value):
    if value is None:
        return None
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result != result.quantize(Decimal('.01')):
            raise ValueError('Amounts must be finite values in cents')
        return result
    except (InvalidOperation, TypeError):
        raise ValueError('Amounts must be finite values in cents') from None


def assess(items, subtotal, tax, total, *, charges=0, discount=0):
    """Items allocate the subtotal; included taxes are never added again."""
    reasons = []
    try:
        values = [money(i.get('line_total')) for i in items]
        subtotal, tax, total = map(money, (subtotal, tax, total))
        charges, discount = money(charges), money(discount)
        if not values or any(v is None for v in values):
            reasons.append('missing_item_amounts')
        item_total = sum((v for v in values if v is not None), Decimal(0))
        if subtotal is None:
            reasons.append('missing_allocation_subtotal')
        item_diff = item_total - subtotal if subtotal is not None else None
        if item_diff is not None and abs(item_diff) > Decimal('.01'):
            reasons.append('item_total_mismatch')
        header_diff = None
        if subtotal is not None and total is not None and tax is not None:
            header_diff = subtotal + tax + charges - discount - total
            if abs(header_diff) > Decimal('.01'):
                reasons.append('header_total_mismatch')
        else:
            reasons.append('unverified_header_total')
        return {'reconciled': not reasons, 'reasons': reasons,
                'item_total': str(item_total), 'subtotal': str(subtotal) if subtotal is not None else None,
                'item_difference': str(item_diff) if item_diff is not None else None,
                'header_difference': str(header_diff) if header_diff is not None else None}
    except ValueError:
        return {'reconciled': False, 'reasons': ['invalid_allocation_amount'],
                'item_total': None, 'subtotal': None, 'item_difference': None, 'header_difference': None}
