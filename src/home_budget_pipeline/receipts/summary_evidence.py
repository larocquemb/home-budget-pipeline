"""Resolve summary fields across retained OCR readings without majority guesses."""
import json

from .. import telemetry
from .datetime import transaction_datetime_evidence
from .payment import card_last4_evidence

POLICY = 'cross-pass-summary-evidence-v1'


def resolve(text, observations):
    """Keep field candidates and pass/line references; equal-purpose conflicts review."""
    sources = []
    for observation in observations:
        if observation.get('status') not in (None, 'success'):
            continue
        provenance = observation.get('provenance') or {}
        if isinstance(provenance, str):
            provenance = json.loads(provenance)
        reading = observation.get('text') or observation.get('extracted_text') or '\n'.join(
            line['text'] for line in provenance.get('lines', []) if isinstance(line.get('text'), str))
        if reading:
            sources.append((reading, {k: observation.get(k) for k in
                            ('run_uuid', 'pass_id', 'page_number', 'engine', 'variant')}))
    # The consensus can contain spliced errors. Prefer original observations
    # whenever available; legacy/text-only inputs retain their ordinary parser.
    if not sources:
        return {}
    result = {}
    for field, extractor in [('transaction_datetime', transaction_datetime_evidence),
                             ('card_last4', card_last4_evidence)]:
        candidates = [{**candidate, 'source': source} for reading, source in sources
                      for candidate in extractor(reading)]
        if not candidates:
            continue
        eligible = candidates
        if field == 'transaction_datetime':
            rank = min(c['purpose_rank'] for c in candidates)
            eligible = [c for c in candidates if c['purpose_rank'] == rank]
            # Same-day time disagreements preserve the date, but remain review.
            dates = {c['value'][:10] for c in eligible}
            timed = {c['value'] for c in eligible if 'T' in c['value']}
            value = next(iter(timed)) if len(dates) == 1 and len(timed) == 1 else (
                next(iter(dates)) if len(dates) == 1 else None)
            conflict = len(dates) > 1 or len(timed) > 1
        else:
            values = {c['value'] for c in eligible}
            value = next(iter(values)) if len(values) == 1 else None
            conflict = len(values) > 1
        result[field] = {'policy': POLICY, 'value': value,
                         'status': 'conflict' if conflict else 'selected',
                         'reason': 'conflicting_original_readings' if conflict else 'unambiguous_original_readings',
                         'candidates': candidates}
    return result


def apply(receipt, observations):
    from .datetime import date_part
    # Trace outcomes without placing card digits or receipt dates in logs.
    with telemetry.span('receipt.summary.field_selection', {'extraction.field_policy': POLICY}) as span:
        decisions = resolve(receipt.text, observations)
        for name, decision in decisions.items():
            span.set_attribute('extraction.' + name + '.status', decision['status'])
            span.set_attribute('extraction.' + name + '.candidate_count', len(decision['candidates']))
    receipt.field_decisions = decisions
    if 'transaction_datetime' in decisions:
        value = decisions['transaction_datetime']['value']
        receipt.transaction_datetime = value
        receipt.transaction_date = date_part(value)
    if 'card_last4' in decisions:
        receipt.card_last4 = decisions['card_last4']['value']
