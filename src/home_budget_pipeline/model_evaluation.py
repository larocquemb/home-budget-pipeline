"""Local, read-only receipt model evaluations against explicit reference labels."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
import re
from pathlib import Path
import statistics
import subprocess
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import uuid

from . import receipt_model_providers as providers
from .gpu_sampling import GpuSampler
from .receipt_shared_evidence import MAX_SOURCE_BYTES, render_images

PROMPT = (
    'Extract the receipt from the supplied evidence images. Preserve every purchase row, '
    'including duplicate purchases, discounts and negative returns. Copy item names as '
    'printed; do not invent products. Use YYYY-MM-DD for transaction_date. '
    'Amounts are signed decimal currency values. Use null for unreadable or absent '
    'summary fields and categories you cannot determine. Do not force arithmetic to '
    'balance by inventing items. Return only the requested JSON.'
)
SUMMARY_FIELDS = ('merchant', 'transaction_date', 'payment_method', 'subtotal', 'tax', 'total')
SCORING_VERSION = 'receipt-financial-v2'
SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': [*SUMMARY_FIELDS, 'items'],
    'properties': {
        **{key: {'type': ['string', 'null']} for key in SUMMARY_FIELDS[:3]},
        **{key: {'type': ['number', 'null']} for key in SUMMARY_FIELDS[3:]},
        'items': {'type': 'array', 'items': {
            'type': 'object', 'additionalProperties': False,
            'required': ['item_name', 'line_total', 'category'],
            'properties': {'item_name': {'type': 'string'},
                           'line_total': {'type': ['number', 'null']},
                           'category': {'type': ['string', 'null']}}}},
    },
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def normalized(value):
    return ' '.join(str(value).casefold().split())


def cents(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value))
        return number.quantize(Decimal('.01')) if number.is_finite() else None
    except InvalidOperation:
        return None


def valid_contract(output):
    """Enforce the extraction contract independently of Ollama's format hint."""
    if not isinstance(output, dict) or set(output) != set(SCHEMA['required']):
        return False
    for key in SUMMARY_FIELDS[:3]:
        if output[key] is not None and not isinstance(output[key], str):
            return False
    for key in SUMMARY_FIELDS[3:]:
        value = output[key]
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
            return False
    if not isinstance(output['items'], list):
        return False
    for item in output['items']:
        if not isinstance(item, dict) or set(item) != {'item_name', 'line_total', 'category'}:
            return False
        if not isinstance(item['item_name'], str) or not item['item_name'].strip():
            return False
        if item['category'] is not None and not isinstance(item['category'], str):
            return False
        value = item['line_total']
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
            return False
    return True


def validate_reference(expected):
    """Reject malformed answer keys rather than scoring invalid labels as null."""
    if not isinstance(expected, dict) or not expected or set(expected) - {*SUMMARY_FIELDS, 'items'}:
        raise ValueError('Reference must contain only supported summary fields and items')
    for key in SUMMARY_FIELDS:
        if key not in expected or expected[key] is None:
            continue
        value = expected[key]
        if key in SUMMARY_FIELDS[:3]:
            valid = isinstance(value, str) and bool(value.strip())
        else:
            valid = type(value) in (int, float) and math.isfinite(value)
        if not valid:
            raise ValueError(f'Invalid reference value for {key}')
    items = expected.get('items', [])
    if not isinstance(items, list):
        raise ValueError('Reference items must be an array')
    for row in items:
        if not isinstance(row, dict):
            raise ValueError('Reference item must be an object')
        names = row.get('aliases', [row.get('item_name')])
        if not isinstance(names, list) or not names or any(not isinstance(name, str) or not name.strip() for name in names):
            raise ValueError('Reference items require an item_name or nonempty aliases')
        amount = row.get('line_total')
        if 'line_total' not in row or amount is not None and (type(amount) not in (int, float) or not math.isfinite(amount)):
            raise ValueError('Reference item requires a numeric or null line_total')
        if row.get('category') is not None and not isinstance(row['category'], str):
            raise ValueError('Reference category must be a string or null')


def score(output, expected, complete_items=False, financial_rules=None):
    """Missing labels are unscored. Each actual row can satisfy one reference row."""
    checks = []
    for key in SUMMARY_FIELDS:
        if key not in expected:
            continue
        actual, reference = output.get(key), expected[key]
        match = (cents(actual) == cents(reference) if key in SUMMARY_FIELDS[3:]
                 else actual is None if reference is None
                 else normalized(actual) == normalized(reference) if actual is not None else False)
        checks.append({'field': key, 'expected': reference, 'actual': actual, 'match': match})
    # Prefer constrained/category-labelled references over less constrained ones.
    references = sorted(expected.get('items', []), key=lambda row: 'category' not in row)
    actual_items = output['items']
    edges = []
    for reference in references:
        names = {normalized(name) for name in reference.get('aliases', [reference.get('item_name', '')])}
        edges.append([index for index, row in enumerate(actual_items)
                      if normalized(row['item_name']) in names
                      and cents(row['line_total']) == cents(reference['line_total'])])
    # Maximum one-to-one matching avoids alias order consuming a unique match
    # needed by another purchase row. Duplicate rows remain separate vertices.
    assigned = {}
    def assign(reference_index, seen):
        for actual_index in edges[reference_index]:
            if actual_index in seen:
                continue
            seen.add(actual_index)
            if actual_index not in assigned or assign(assigned[actual_index], seen):
                assigned[actual_index] = reference_index
                return True
        return False
    for reference_index in range(len(references)):
        assign(reference_index, set())
    by_reference = {reference_index: actual_index for actual_index, reference_index in assigned.items()}
    matched, missing = [], []
    for index, reference in enumerate(references):
        if index not in by_reference:
            missing.append(reference)
        else:
            matched.append({'expected': reference, 'actual': actual_items[by_reference[index]]})
    # Extra rows are only discrepancies when the answer key covers the whole receipt.
    unexpected = [row for index, row in enumerate(actual_items) if index not in assigned] if complete_items else None
    subtotal, tax, total = (cents(output.get(key)) for key in ('subtotal', 'tax', 'total'))
    arithmetic = None if None in (subtotal, tax, total) else subtotal + tax == total
    category_checks = [{'expected': reference['category'],
                        'actual': actual_items[by_reference[index]]['category'] if index in by_reference else None,
                        'match': index in by_reference and actual_items[by_reference[index]]['category'] == reference['category']}
                       for index, reference in enumerate(references) if 'category' in reference]
    result = {'summary_checks': checks, 'summary_matches': sum(c['match'] for c in checks),
            'summary_scored': len(checks), 'known_items_matched': len(matched),
            'known_items_scored': len(references), 'matched_items': matched,
            'missing_items': missing, 'unexpected_items': unexpected,
            'complete_item_reference': complete_items,
            'categories_scored': sum('category' in row for row in references),
            'categories_matched': sum(check['match'] for check in category_checks),
            'category_checks': category_checks,
            'subtotal_plus_tax_matches_total': arithmetic,
            'item_sum_minus_subtotal': float(sum((cents(row['line_total']) or Decimal(0)
                for row in output['items']), Decimal(0)) - subtotal)
                if subtotal is not None and all(row['line_total'] is not None for row in output['items']) else None}
    signed_sum = (sum((cents(row['line_total']) for row in actual_items), Decimal(0))
                  if all(row['line_total'] is not None for row in actual_items) else None)
    rules = financial_rules or {}
    reference_subtotal = cents(expected.get('subtotal'))
    forbidden = [row for row in actual_items if any(
        re.search(pattern, row['item_name'], re.IGNORECASE)
        for pattern in rules.get('non_monetary_patterns', []))]
    fees = rules.get('required_fees', [])
    fee_quality = score(output, {'items': fees}) if fees else None
    result['financial'] = {
        'signed_item_sum': float(signed_sum) if signed_sum is not None else None,
        'item_sum_matches_reported_subtotal': signed_sum == subtotal if signed_sum is not None and subtotal is not None else None,
        'item_sum_matches_reference_subtotal': (signed_sum == reference_subtotal if signed_sum is not None else False)
            if rules.get('reconcile_items_to_subtotal') else None,
        'reference_subtotal': expected.get('subtotal') if rules.get('reconcile_items_to_subtotal') else None,
        'non_monetary_rows': forbidden if 'non_monetary_patterns' in rules else None,
        'fees_matched': fee_quality['known_items_matched'] if fee_quality else None,
        'fees_scored': len(fees) if 'required_fees' in rules else None,
        'missing_fees': fee_quality['missing_items'] if fee_quality else [],
    }
    return result


def load_reference(entry, manifest):
    expected = entry.get('expected', {})
    reference_hash, metadata = None, {}
    if entry.get('expected_file'):
        raw = (manifest.parent / entry['expected_file']).read_bytes()
        reference = json.loads(raw)
        reference_hash = digest(raw)
        expected = reference['expected']
        metadata = {key: reference[key] for key in
                    ('source_sha256', 'verification', 'unscored_fields', 'tax_components', 'financial_rules')
                    if key in reference}
    elif entry.get('expected_fixture'):
        raw = (manifest.parent / entry['expected_fixture']).read_bytes()
        fixture = json.loads(raw)
        reference_hash = digest(raw)
        expected = dict(fixture['expected'])
        expected['items'] = [{'item_name': row['item_name'], 'line_total': row['line_total']}
                             for row in fixture['detected_items']]
        for row in fixture['items_to_find']:
            expected['items'].extend({'aliases': row['ocr_names'], 'line_total': amount}
                                     for amount in row['line_totals'])
    validate_reference(expected)
    rules = metadata.get('financial_rules', {})
    if not isinstance(rules, dict):
        raise ValueError('Financial rules must be an object')
    if rules.get('reconcile_items_to_subtotal') and cents(expected.get('subtotal')) is None:
        raise ValueError('Reconciliation requires a verified subtotal')
    if 'reconcile_items_to_subtotal' in rules and not isinstance(rules['reconcile_items_to_subtotal'], bool):
        raise ValueError('Reconciliation rule must be boolean')
    if 'required_fees' in rules:
        validate_reference({'items': rules['required_fees']})
    patterns = rules.get('non_monetary_patterns', [])
    if not isinstance(patterns, list) or any(not isinstance(pattern, str) for pattern in patterns):
        raise ValueError('Non-monetary patterns must be an array of strings')
    for pattern in patterns:
        re.compile(pattern, re.IGNORECASE)
    if metadata.get('tax_components'):
        values = metadata['tax_components'].values()
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
            raise ValueError('Tax components must be finite numbers')
        if sum((cents(value) for value in metadata['tax_components'].values()), Decimal(0)) != cents(expected.get('tax')):
            raise ValueError('Verified tax components must sum to the combined tax reference')
    return expected, reference_hash, metadata



def get_json(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as response:
        return json.load(response)


def load_cases(manifest, receipt_root):
    document = json.loads(manifest.read_text())
    cases = []
    for entry in document['cases']:
        source = (receipt_root / entry['source']).resolve()
        if not source.is_relative_to(receipt_root.resolve()) or not source.is_file():
            raise ValueError(f"Missing receipt for {entry['id']}; set RECEIPT_ROOT to its parent directory")
        if source.stat().st_size > MAX_SOURCE_BYTES:
            raise ValueError(f"Receipt {entry['id']} exceeds the image renderer's source limit")
        source_bytes = source.read_bytes()
        expected, reference_hash, metadata = load_reference(entry, manifest)
        if metadata.get('source_sha256') and metadata['source_sha256'] != digest(source_bytes):
            raise ValueError(f"Receipt source hash differs from verified reference for {entry['id']}")
        complete = entry.get('complete_items', False)
        if not isinstance(complete, bool) or not expected:
            raise ValueError('Cases require explicit reference labels and boolean complete_items')
        validate_reference(expected)
        pages = entry.get('pages', [1])
        if not pages or len(pages) > 2 or any(type(page) is not int or page < 1 for page in pages):
            raise ValueError('Choose one or two positive page numbers per case')
        # Reuse production image hashing, sizing and encoding.
        images, errors = render_images({'documents': [{'id': 1, 'source_reference': entry['source'],
            'source_sha256': digest(source_bytes)}],
            'geometry': [{'evidence_id': 1, 'page_number': page} for page in pages]}, root=receipt_root)
        if errors or len(images) != len(pages):
            raise ValueError(f"Cannot render selected receipt pages for {entry['id']}: {errors}")
        cases.append({'id': entry['id'], 'split': entry.get('split', 'development'),
                      'source_sha256': digest(source_bytes), 'reference_sha256': reference_hash,
                      'expected': expected, 'complete_items': complete, 'images': images,
                      'reference_metadata': metadata})
    if not cases or len({case['id'] for case in cases}) != len(cases):
        raise ValueError('Supply one or more cases with unique IDs')
    return cases


def model_snapshot(base, model):
    rows = get_json(base, '/api/tags')['models']
    found = next((row for row in rows if model in (row.get('name'), row.get('model'))), None)
    if found is None:
        raise ValueError(f'Model {model} is not installed at {base}; pull it explicitly before evaluation')
    details = providers.post(base + '/api/show', {'model': model})
    if 'vision' not in details.get('capabilities', []):
        raise ValueError(f'Model {model} does not advertise vision support')
    return {'name': model, 'digest': found['digest'], 'size_bytes': found.get('size'),
            'details': details.get('details'), 'parameters': details.get('parameters'),
            'template_sha256': digest(details.get('template', '').encode()),
            'capabilities': details.get('capabilities')}


def report_markdown(report):
    incomplete = any(row['status'] != 'ok' for row in report['runs'])
    state = ('In progress / partial' if 'finished_at' not in report else
             'Incomplete' if incomplete or report.get('model_digests_unchanged') is not True else
             'Completed; quality requires review')
    lines = ['# Receipt model comparison', '', f"Run: `{report['run_id']}`", '',
             f'Status: **{state}**', '',
             'Quality differences are informational. This report does not change production.', '',
             'Partial reference labels cannot establish overall receipt accuracy or hallucination rates.', '',
             '| Model | Case | Successful runs | Summary matches | Known items matched | Median wall seconds |',
             '|---|---|---:|---|---|---:|']
    for model in report['models']:
        for case in report['cases']:
            rows = [row for row in report['runs'] if row['model'] == model['name'] and row['case'] == case['id']]
            good = [row for row in rows if row['status'] == 'ok']
            summary = ', '.join(f"{row['quality']['summary_matches']}/{row['quality']['summary_scored']}" for row in good) or 'unscored'
            items = ', '.join(f"{row['quality']['known_items_matched']}/{row['quality']['known_items_scored']}" for row in good) or 'unscored'
            wall = f"{statistics.median(row['wall_seconds'] for row in good):.2f}" if good else '—'
            lines.append(f"| {model['name']} | {case['id']} | {len(good)}/{len(rows)} | {summary} | {items} | {wall} |")
    if report.get('rescore_of'):
        lines.extend(['', f"Rescored saved outputs from `{report['rescore_of']['run_id']}`; no new inference was performed."])
    lines.extend(['', '## Financial correctness', '',
                  '| Model | Case | Repeat | Tax matches reference | Rows reconcile to verified subtotal | Fees matched | Non-monetary rows included |',
                  '|---|---|---:|---|---|---|---:|'])
    def verdict(value):
        return 'unscored' if value is None else 'PASS' if value else 'FAIL'
    for row in report['runs']:
        if row['status'] != 'ok':
            continue
        quality = row['quality']
        financial = quality.get('financial', {})
        tax = next((check['match'] for check in quality['summary_checks'] if check['field'] == 'tax'), None)
        fees = (f"{financial['fees_matched']}/{financial['fees_scored']}"
                if financial.get('fees_scored') is not None else 'unscored')
        exclusions = financial.get('non_monetary_rows')
        lines.append(f"| {row['model']} | {row['case']} | {row['repeat']} | {verdict(tax)} | "
                     f"{verdict(financial.get('item_sum_matches_reference_subtotal'))} | {fees} | "
                     f"{len(exclusions) if exclusions is not None else 'unscored'} |")
    lines.extend(['', '## Reference coverage', ''])
    for case in report['cases']:
        metadata = case.get('reference_metadata', {})
        for field, reason in metadata.get('unscored_fields', {}).items():
            lines.append(f"- {case['id']}: {field} unscored — {reason}")
    lines.extend(['', '## Repeat consistency', ''])
    for model in report['models']:
        for case in report['cases']:
            outputs = [json.dumps(row['output'], sort_keys=True) for row in report['runs']
                       if row['model'] == model['name'] and row['case'] == case['id'] and row['status'] == 'ok']
            counts = Counter(outputs)
            lines.append(f"- {model['name']} / {case['id']}: {len(counts)} distinct outputs across {len(outputs)} successful runs.")
    lines.extend(['', '## Discrepancies and runtime errors', ''])
    for row in report['runs']:
        prefix = f"{row['model']} / {row['case']} / repeat {row['repeat']}"
        if row['status'] != 'ok':
            lines.append(f"- {prefix}: {row['error_type']}; see report.json for diagnostics.")
            continue
        quality = row['quality']
        for check in quality['summary_checks']:
            if not check['match']:
                lines.append(f"- {prefix}: {check['field']} expected {check['expected']!r}, got {check['actual']!r}.")
        if quality['missing_items']:
            lines.append(f"- {prefix}: {len(quality['missing_items'])} known item rows did not match; inspect report.json.")
        if quality['unexpected_items']:
            lines.append(f"- {prefix}: {len(quality['unexpected_items'])} unexpected rows against a complete reference.")
        if quality['categories_matched'] < quality['categories_scored']:
            lines.append(f"- {prefix}: {quality['categories_scored'] - quality['categories_matched']} verified categories did not match.")
        if quality['subtotal_plus_tax_matches_total'] is False:
            lines.append(f'- {prefix}: subtotal + tax differs from total (informational).')
        financial = quality.get('financial', {})
        if financial.get('item_sum_matches_reference_subtotal') is False:
            lines.append(f"- {prefix}: signed row sum {financial['signed_item_sum']} differs from verified subtotal {financial['reference_subtotal']}.")
        for item in financial.get('non_monetary_rows') or []:
            lines.append(f"- {prefix}: non-monetary annotation {item['item_name']!r} included as an item with amount {item['line_total']}.")
        if financial.get('missing_fees'):
            lines.append(f"- {prefix}: {len(financial['missing_fees'])} verified fee rows missing.")
    lines.extend(['', '## Review before promotion', '',
                  '- Inspect the saved outputs and discrepancies; no aggregate winner is chosen automatically.',
                  '- Review model digests, prompt/settings, case hashes, and repeat consistency in report.json.',
                  '- Inspect GPU memory and placement snapshots; GPU activity is shared with other host workloads.',
                  '- Compare holdout receipts before changing the production model through GitOps.', ''])
    return '\n'.join(lines)


def rescore_report(source, manifest, output_root):
    """Preserve original outputs/timings and bind new labels to the same evidence."""
    try:
        raw = source.read_bytes()
        report = json.loads(raw)
        entries = {entry['id']: entry for entry in json.loads(manifest.read_text())['cases']}
        for case in report['cases']:
            entry = entries[case['id']]
            expected, reference_hash, metadata = load_reference(entry, manifest)
            if metadata.get('source_sha256') != case['source_sha256']:
                raise ValueError('Offline rescoring requires a verified reference bound to the same source hash')
            if entry.get('pages', [1]) != [image['page_number'] for image in case['images']]:
                raise ValueError('Selected pages differ from the original model inputs')
            case.update(expected=expected, reference_sha256=reference_hash,
                        complete_items=entry.get('complete_items', False), reference_metadata=metadata)
            if not isinstance(case['complete_items'], bool):
                raise ValueError('complete_items must be boolean')
        cases = {case['id']: case for case in report['cases']}
        for row in report['runs']:
            if row['status'] != 'ok':
                continue
            if not valid_contract(row['output']):
                raise ValueError('Saved successful output violates the extraction contract')
            case = cases[row['case']]
            row['quality'] = score(row['output'], case['expected'], case['complete_items'],
                                   case['reference_metadata'].get('financial_rules'))
        report['rescore_of'] = {'run_id': report['run_id'], 'report_sha256': digest(raw)}
        report['run_id'] += '-rescore-' + uuid.uuid4().hex[:8]
        report['rescored_at'] = datetime.now(timezone.utc).isoformat()
        report['scoring_version'] = SCORING_VERSION
        report['scoring_manifest_sha256'] = digest(manifest.read_bytes())
        report['scoring_git_commit'] = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
        directory = output_root / report['run_id']
        directory.mkdir(parents=True, exist_ok=False)
        os.chmod(directory, 0o700)
        for name, content in [('report.json', json.dumps(report, indent=2, allow_nan=False)),
                              ('report.md', report_markdown(report))]:
            target = directory / name
            target.write_text(content)
            os.chmod(target, 0o600)
        print(f'Rescored report: {directory / "report.md"}')
        return 1 if 'finished_at' not in report or any(row['status'] != 'ok' for row in report['runs']) or report.get('model_digests_unchanged') is not True else 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f'Rescoring failed: {exc}')
        return 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline')
    parser.add_argument('--candidate')
    parser.add_argument('--rescore', type=Path, help='Rescore saved outputs without inference or image rendering')
    parser.add_argument('--cases', type=Path, default=Path('evals/receipts/cases.json'))
    parser.add_argument('--receipt-root', type=Path, default=Path('.receipt-test/inbox'))
    parser.add_argument('--output', type=Path, default=Path('.model-evals'))
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--context-tokens', type=int, default=32768)
    parser.add_argument('--output-tokens', type=int, default=8192)
    parser.add_argument('--timeout', type=float, default=600)
    args = parser.parse_args(argv)
    if args.rescore:
        return rescore_report(args.rescore, args.cases, args.output)
    if not args.baseline or not args.candidate:
        parser.error('Supply --baseline and --candidate, or --rescore')
    if args.baseline == args.candidate or not 1 <= args.repeats <= 10:
        parser.error('Use distinct models and 1..10 repeats')
    if not 1024 <= args.context_tokens <= 65536 or not 256 <= args.output_tokens <= 16384 or args.output_tokens >= args.context_tokens or args.timeout <= 0:
        parser.error('Invalid context, output or timeout budget')
    base = os.getenv('OLLAMA_BASE_URL') or os.getenv('OLLAMA_HOST') or 'http://192.168.2.201:11436'
    base = base.rstrip('/')
    address = urlsplit(base)
    if address.scheme not in ('http', 'https') or not address.hostname or address.username or address.password or address.query or address.fragment:
        parser.error('OLLAMA_BASE_URL must be an HTTP(S) URL without credentials, query or fragment')
    os.environ['OLLAMA_BASE_URL'] = base
    os.environ['COMPARISON_TIMEOUT_SECONDS'] = str(args.timeout)
    try:
        cases = load_cases(args.cases, args.receipt_root)
        models = [model_snapshot(base, name) for name in (args.baseline, args.candidate)]
        version = get_json(base, '/api/version')['version']
    except (ValueError, OSError, KeyError) as exc:
        print(f'Evaluation preflight failed: {exc}')
        return 2
    commit = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True)
    dirty = subprocess.run(['git', 'status', '--porcelain'], capture_output=True, text=True)
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    directory = args.output / run_id
    directory.mkdir(parents=True, exist_ok=False)
    os.chmod(directory, 0o700)
    report = {'run_id': run_id, 'scoring_version': SCORING_VERSION, 'started_at': datetime.now(timezone.utc).isoformat(),
              'git_commit': commit.stdout.strip() if commit.returncode == 0 else None,
              'git_dirty': bool(dirty.stdout.strip()), 'ollama_url': base, 'ollama_version': version,
              'models': models, 'prompt': PROMPT, 'prompt_sha256': digest(PROMPT.encode()),
              'schema_sha256': digest(json.dumps(SCHEMA, sort_keys=True).encode()),
              'settings': {'context_tokens': args.context_tokens, 'output_tokens': args.output_tokens,
                           'temperature': 0, 'think': False, 'repeats': args.repeats},
              'manifest_sha256': digest(args.cases.read_bytes()),
              'cases': [{**{key: value for key, value in case.items() if key != 'images'},
                         'images': [{key: value for key, value in image.items() if key != 'data'}
                                    for image in case['images']],
                         'request_prompt_sha256': digest(providers.qwen_content(PROMPT, SCHEMA, case['images']).encode())}
                        for case in cases],
              'runs': []}

    def save():
        for name, content in [('report.json', json.dumps(report, indent=2, allow_nan=False)),
                              ('report.md', report_markdown(report))]:
            target = directory / name
            target.write_text(content)
            os.chmod(target, 0o600)

    save()
    # Group by model to avoid repeated model eviction; same cases and inputs for both.
    for model in models:
        profile = providers.Profile('evaluation', 'qwen', model['name'], args.context_tokens,
                                    args.output_tokens, vision=True)
        for case in cases:
            for repeat in range(1, args.repeats + 1):
                row = {'model': model['name'], 'case': case['id'], 'repeat': repeat}
                print(f"Evaluating {model['name']} / {case['id']} / {repeat}/{args.repeats}", flush=True)
                samples = []
                sampler = GpuSampler(lambda event, value: samples.append(value), {'run_id': run_id})
                started = time.monotonic()
                try:
                    with sampler:
                        response = providers.request(profile, PROMPT, SCHEMA, case['images'])
                    if not valid_contract(response['output']):
                        row['invalid_output'] = json.dumps(response['output'])
                        raise ValueError('Model returned an invalid extraction contract')
                    row.update(status='ok', output=response['output'],
                               usage={key: value for key, value in response.items() if key != 'output'},
                               quality=score(response['output'], case['expected'], case['complete_items'],
                                             case.get('reference_metadata', {}).get('financial_rules')))
                except (ValueError, OSError, KeyError, TypeError) as exc:
                    row.update(status='error', error_type=type(exc).__name__, error_message=str(exc)[:1000])
                    if isinstance(exc, providers.IncompleteModelOutput):
                        row['usage'] = exc.usage
                row['wall_seconds'] = round(time.monotonic() - started, 3)
                row['gpu'] = sampler.summary()
                row['gpu']['peak_memory_bytes'] = max((sample['gpu_memory_bytes'] for sample in samples
                    if isinstance(sample.get('gpu_memory_bytes'), (int, float))), default=None)
                try:
                    row['placement'] = [{key: model.get(key) for key in
                        ('name', 'digest', 'size', 'size_vram', 'context_length')}
                        for model in get_json(base, '/api/ps')['models']]
                except (OSError, ValueError, KeyError):
                    row['placement'] = None
                report['runs'].append(row)
                save()
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    try:
        final_models = [model_snapshot(base, model['name']) for model in models]
        report['model_digests_unchanged'] = all(before['digest'] == after['digest']
                                               for before, after in zip(models, final_models))
    except (OSError, ValueError, KeyError):
        report['model_digests_unchanged'] = None
    save()
    print(f'Report: {directory / "report.md"}')
    return 1 if any(row['status'] == 'error' for row in report['runs']) or report['model_digests_unchanged'] is not True else 0


if __name__ == '__main__':
    raise SystemExit(main())
