"""Informational model comparisons through existing product enrichment functions."""
from __future__ import annotations
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import uuid
from urllib.parse import urlsplit
import urllib.request

from . import product_comparison as comparison
from . import product_enrichment as core
from .categorization.logic import deterministic_category
from .product_labels import product_name


def url_identity(value):
    address = urlsplit(value or '')
    return address.netloc.lower(), address.path.rstrip('/')


def qualify(result, case):
    """Heuristic acceptance and human reference agreement are separate evidence."""
    complete = result.get('status') in ('matched', 'no_match')
    label = product_name(result.get('candidate_title'), result.get('candidate_url')) if complete else None
    category, category_source = deterministic_category(label, source='scanned', merchant=case['merchant']) if label and result.get('accepted') else (None, None)
    return {'product_name': label, 'category': category, 'category_source': category_source,
            'category_scope': 'built-in deterministic rules; no database overrides or AI categorization',
            'reference_product_match': (url_identity(result.get('candidate_url')) == url_identity(case['expected_product_url']))
                if complete and case.get('expected_product_url') else None,
            'reference_category_match': category == case['expected_category']
                if complete and 'expected_category' in case else None}


def markdown(report):
    lines = ['# Informational product enrichment comparison', '',
             f"Run: `{report['run_id']}`", '',
             '**Informational only. Model disagreements and incomplete API requests do not fail CI.**', '',
             ('Uses the production collaborate_item path with real OCR/image evidence, descriptor/brand expansion, retries and citation review.' if report.get('workflow') == 'collaboration' else 'Uses the older paired enrichment experiment.'), '',
             'Categories use built-in deterministic application rules on accepted product labels. Database overrides and AI category fallback are not evaluated.', '',
             '| Case | Model | Status | Product | Reference product | Category | Reference category | Confidence heuristic | Seconds |',
             '|---|---|---|---|---|---|---|---:|---:|']
    def verdict(value):
        return 'unscored' if value is None else 'match' if value else 'different'
    def cell(value):
        return str(value if value is not None else '—').replace('|', '\\|').replace('\n', ' ')
    for row in report['results']:
        q = row['qualification']
        status = 'incomplete' if row.get('status') == 'error' else row.get('status')
        lines.append('| ' + ' | '.join(cell(value) for value in (
            row['case_id'], row['model'], status, q['product_name'], verdict(q['reference_product_match']),
            q['category'], verdict(q['reference_category_match']), row.get('confidence'), row.get('seconds'))) + ' |')
    lines.extend(['', '## Supporting evidence and proposals', ''])
    if report.get('preflight_reason'):
        lines.extend([f"Setup incomplete: {report['preflight_reason']}.", ''])
    for row in report['results']:
        lines.extend([f"### {row['case_id']} / {row['model']}", '',
                      f"- Proposals: {json.dumps(row.get('queries', []))}",
                      f"- Selected retailer URL: {row.get('candidate_url') or 'none'}",
                      f"- Selected title: {row.get('candidate_title') or 'none'}",
                      f"- Origin: {row.get('candidate_origin') or 'none'}; accepted by heuristic: {row.get('accepted', False)}."])
        if row.get('error_type'):
            lines.append(f"- Incomplete: {row['error_type']}; not a model loss.")
    lines.extend(['', 'Inspect report.json for shared baseline evidence, exact prompts, model digests, token timings, candidate evidence and all query results. Confidence is a lexical heuristic, not a correctness probability. No automatic winner or production promotion.', ''])
    return '\n'.join(lines)


def run(cases, models, api_key, threshold=.85):
    """No database/cache writes; shared search results are scoped per original item."""
    results, evidence = [], []
    for case in cases:
        cache = {}
        try:
            baseline = comparison.baseline_search(case['receipt_item'], case['merchant'], api_key, cache)
            baseline_error = None
        except Exception as exc:
            baseline, baseline_error = [], type(exc).__name__
        # Same immutable baseline and prompt for each model; each may retrieve
        # its own proposals, but cannot borrow another model's new candidates.
        candidates = {}
        for _, candidate in sorted(baseline, key=lambda entry: entry[1].score, reverse=True):
            candidates.setdefault(candidate.url, candidate)
        prompt = comparison.prompt(case['receipt_item'], case['merchant'], list(candidates.values())[:5])
        for model in models:
            if baseline_error:
                result = {'model': model, 'status': 'error', 'error_type': baseline_error,
                          'confidence': None, 'accepted': False}
            else:
                result = comparison.evaluate('qwen', case['receipt_item'], case['merchant'], api_key,
                                             threshold, cache, baseline, model=model)
            result.update(case_id=case['id'], qualification=qualify(result, case))
            results.append(result)
        evidence.append({'case_id': case['id'], 'prompt': prompt,
                         'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
                         'baseline': [{'query': query, **asdict(result)} for query, result in baseline],
                         'query_results': {query: [asdict(result) for result in rows] for query, rows in cache.items()}})
    return results, evidence


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', default='qwen3-vl-receipts:30b-instruct')
    parser.add_argument('--candidate', default='qwen3.8:27b')
    parser.add_argument('--cases', type=Path, default=Path('evals/enrichment/cases.json'))
    parser.add_argument('--output', type=Path, default=Path('.model-evals'))
    parser.add_argument('--threshold', type=float, default=.85)
    parser.add_argument('--workflow', choices=('collaboration', 'paired'), default='collaboration')
    parser.add_argument('--receipt-root', type=Path, help='Local copies of hash-verified production source files')
    args = parser.parse_args(argv)
    if args.baseline == args.candidate or not 0 <= args.threshold <= 1:
        parser.error('Use distinct models and a threshold between 0 and 1')
    base = os.getenv('OLLAMA_BASE_URL') or os.getenv('OLLAMA_HOST') or 'http://192.168.2.201:11436'
    base = base.rstrip('/')
    address = urlsplit(base)
    if address.scheme not in ('http', 'https') or not address.hostname or address.username or address.password or address.query or address.fragment:
        parser.error('Use an HTTP(S) Ollama URL without embedded credentials')
    os.environ['OLLAMA_BASE_URL'] = base
    manifest = json.loads(args.cases.read_text())
    cases = manifest['cases']
    if not cases or len({case['id'] for case in cases}) != len(cases) or any(
        not case.get('receipt_item') or not core.retailer_domain(case.get('merchant', '')) for case in cases):
        parser.error('Supply unique cases with receipt text and a supported retailer')
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-enrichment-' + uuid.uuid4().hex[:8]
    report = {'run_id': run_id, 'started_at': datetime.now(timezone.utc).isoformat(),
              'git_commit': subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip(),
              'manifest_sha256': hashlib.sha256(args.cases.read_bytes()).hexdigest(),
              'reference_note': manifest.get('reference_note'), 'workflow': args.workflow,
              'cases': cases, 'models': [], 'results': [], 'search_evidence': [],
              'schema_sha256': hashlib.sha256(json.dumps(comparison.QUERY_SCHEMA, sort_keys=True).encode()).hexdigest(),
              'ollama_url': base, 'threshold': args.threshold,
              'settings': {'temperature': 0, 'think': False, 'output_tokens': 1200,
                           'context_tokens': int(os.getenv('OLLAMA_CONTEXT_TOKENS', '4096'))}}
    key = os.getenv('BRAVE_SEARCH_API_KEY', '').strip()
    try:
        if not key:
            raise ValueError('missing_brave_credentials')
        with urllib.request.urlopen(base + '/api/tags', timeout=10) as response:
            installed = json.load(response)['models']
        for model in (args.baseline, args.candidate):
            found = next((row for row in installed if model in (row.get('name'), row.get('model'))), None)
            if not found:
                raise ValueError('model_not_installed')
            report['models'].append({'name': model, 'digest': found['digest']})
        if args.workflow == 'collaboration':
            from . import collaboration_evaluation as production
            if args.receipt_root:
                os.environ['HOME_BUDGET_RECEIPTS_ROOT'] = str(args.receipt_root.resolve())
            inputs = production.load_inputs(cases, os.getenv('DATABASE_URL') or os.getenv('HOME_BUDGET_PG_DSN'))
            context_tokens = int(os.getenv('QWEN_CONTEXT_TOKENS') or '32768')
            output_tokens = int(os.getenv('QWEN_OUTPUT_TOKENS') or '8192')
            if not 1024 <= context_tokens <= 65536 or not 256 <= output_tokens <= 16384 or output_tokens >= context_tokens:
                raise ValueError('invalid_collaboration_budgets')
            report['settings'].update(context_tokens=context_tokens, output_tokens=output_tokens,
                                      profile_policy='one vision-enabled Qwen profile per independent model run',
                                      descriptor_expansion={'temperature': 0.7, 'top_p': 0.8, 'top_k': 20, 'seed': 42})
            report['schema_sha256'] = None  # Production derives stage schemas and citation enums dynamically.
            report['results'], report['search_evidence'] = production.run(
                cases, [args.baseline, args.candidate], key, args.threshold, inputs, context_tokens, output_tokens)
            for row in report['results']:
                row['qualification'] = qualify(row, next(case for case in cases if case['id'] == row['case_id']))
        else:
            report['results'], report['search_evidence'] = run(cases, [args.baseline, args.candidate], key, args.threshold)
    except Exception as exc:
        report['preflight_error_type'] = type(exc).__name__
        report['preflight_reason'] = ('missing_brave_credentials' if not key else 'evaluation_setup_unavailable')
        report['results'] = [{'case_id': case['id'], 'model': model, 'status': 'error',
                              'error_type': type(exc).__name__, 'confidence': None,
                              'qualification': qualify({'status': 'error'}, case)}
                             for case in cases for model in (args.baseline, args.candidate)]
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    report['incomplete'] = any(row['status'] == 'error' for row in report['results'])
    directory = args.output / run_id
    directory.mkdir(parents=True, exist_ok=False)
    os.chmod(directory, 0o700)
    for name, content in [('report.json', json.dumps(report, indent=2, default=str)), ('report.md', markdown(report))]:
        target = directory / name
        target.write_text(content)
        os.chmod(target, 0o600)
    print(f'Informational report: {directory / "report.md"}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
