#!/usr/bin/env python3
"""Shared AI category fallback engine for item classification."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

from .logic import CATEGORY_ORDER, canonicalize_category, normalize_for_match


class AICategoryEngine:
    def __init__(self, suggestions_path: Path) -> None:
        self.suggestions_path = suggestions_path
        self.enabled = os.getenv('ENABLE_AI_CATEGORY_FALLBACK', '1').lower() in {'1', 'true', 'yes'}
        self.confidence_threshold = float(os.getenv('AI_CONFIDENCE_THRESHOLD', '0.65'))
        self.timeout_seconds = float(os.getenv('AI_TIMEOUT_SECONDS', '12'))
        self.client_max_retries = int(os.getenv('AI_CLIENT_MAX_RETRIES', '1'))
        self.max_calls_per_run = int(os.getenv('AI_MAX_CALLS_PER_RUN', '2000'))
        self.batch_size = int(os.getenv('AI_BATCH_SIZE', '8'))
        self.slow_call_seconds = float(os.getenv('AI_SLOW_CALL_SECONDS', '8'))
        self.runtime_disabled = False
        self.client = None
        self.cache: Dict[str, Optional[str]] = {}
        self.consecutive_errors = 0
        self.max_consecutive_errors = 3
        self.stats = {
            'calls': 0, 'ai_cache_hits': 0, 'success': 0, 'no_result': 0,
            'api_errors': 0, 'call_seconds_total': 0.0, 'call_seconds_max': 0.0,
            'call_items_max': 0, 'slow_calls': 0, 'duration_le_2s': 0,
            'duration_2_to_5s': 0, 'duration_5_to_8s': 0, 'duration_gt_8s': 0,
            'batch_histogram': {}, 'skipped_call_budget': 0, 'batch_items': 0,
        }

    def load_suggestions(self) -> None:
        path = self.suggestions_path
        if not path.exists():
            self.cache = {}
            return
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(payload, dict):
                self.cache = {}
                return
            cleaned: Dict[str, Optional[str]] = {}
            for key, value in payload.items():
                normalized = canonicalize_category(value)
                if isinstance(key, str) and normalized in CATEGORY_ORDER:
                    cleaned[key] = normalized
            self.cache = cleaned
        except Exception:
            self.cache = {}

    def save_suggestions(self) -> None:
        try:
            persistent = {k: v for k, v in self.cache.items() if v in CATEGORY_ORDER}
            self.suggestions_path.write_text(
                json.dumps(persistent, ensure_ascii=True, indent=2, sort_keys=True), encoding='utf-8'
            )
        except Exception:
            return

    def cache_key(self, desc: str) -> str:
        return normalize_for_match(desc)

    def get_cached_category(self, desc: str) -> Optional[str]:
        return self.get_cached_category_by_key(self.cache_key(desc))

    def get_cached_category_by_key(self, key: str) -> Optional[str]:
        normalized = canonicalize_category(self.cache.get(key))
        if key in self.cache and normalized in CATEGORY_ORDER:
            self.stats['ai_cache_hits'] += 1
            return normalized
        return None

    def set_cached_category(self, desc: str, category: Optional[str]) -> None:
        self.cache[self.cache_key(desc)] = category

    def set_cached_category_by_key(self, key: str, category: Optional[str]) -> None:
        self.cache[key] = category

    def _ai_hint_text(self, desc: str) -> str:
        expanded = desc
        repl = {
            r'\bckn\b': 'chicken', r'\bchkn\b': 'chicken', r'\bbnls\b': 'boneless',
            r'\bygrt\b': 'yogurt', r'\bbtr\b': 'butter', r'\bssg\b': 'sausage',
            r'\bflngs\b': 'fillets', r'\bbcn\b': 'bacon',
        }
        for pat, val in repl.items():
            expanded = re.sub(pat, val, expanded, flags=re.I)
        return re.sub(r'\s+', ' ', expanded).strip()

    def _get_client(self):
        api_key = os.getenv('OPENAI_API_KEY', '').strip()
        if not api_key:
            return None
        if self.client is None:
            try:
                from openai import OpenAI  # type: ignore
            except Exception:
                return None
            self.client = OpenAI(api_key=api_key, max_retries=self.client_max_retries,
                                 timeout=self.timeout_seconds)
        return self.client

    def _record_call_metrics(self, elapsed: float, item_count: int) -> None:
        self.stats['call_seconds_total'] += elapsed
        if elapsed > self.stats['call_seconds_max']:
            self.stats['call_seconds_max'] = elapsed
        if item_count > self.stats['call_items_max']:
            self.stats['call_items_max'] = item_count
        if elapsed <= 2:
            self.stats['duration_le_2s'] += 1
        elif elapsed <= 5:
            self.stats['duration_2_to_5s'] += 1
        elif elapsed <= 8:
            self.stats['duration_5_to_8s'] += 1
        else:
            self.stats['duration_gt_8s'] += 1
        if elapsed >= self.slow_call_seconds:
            self.stats['slow_calls'] += 1
        batch_key = str(item_count)
        hist = self.stats['batch_histogram']
        hist[batch_key] = hist.get(batch_key, 0) + 1

    def chunk_list(self, items: List[Dict[str, str]]) -> List[List[Dict[str, str]]]:
        chunk_size = self.batch_size
        if chunk_size <= 0:
            return [items] if items else []
        return [items[i:i + chunk_size] for i in range(0, len(items), chunk_size)]

    def try_category_batch(self, items: List[Dict[str, str]],
                           source_label: str = 'item') -> Dict[str, Optional[str]]:
        if not self.enabled or self.runtime_disabled:
            return {}
        if not items:
            return {}
        if self.stats['calls'] >= self.max_calls_per_run:
            self.stats['skipped_call_budget'] += len(items)
            self.stats['no_result'] += len(items)
            return {}
        try:
            from openai import APITimeoutError, APIConnectionError, AuthenticationError, RateLimitError  # type: ignore
        except Exception:
            return {}
        client = self._get_client()
        if client is None:
            return {}
        schema = {
            'type': 'object', 'additionalProperties': False, 'required': ['results'],
            'properties': {'results': {'type': 'array', 'items': {
                'type': 'object', 'additionalProperties': False,
                'required': ['id', 'category', 'confidence'],
                'properties': {
                    'id': {'type': 'string'},
                    'category': {'type': 'string', 'enum': CATEGORY_ORDER},
                    'confidence': {'type': 'number', 'minimum': 0.0, 'maximum': 1.0},
                },
            }}},
        }
        lines: List[str] = []
        for item in items:
            lines.append(
                f'- id={item["id"]} | sku={item.get("sku", "") or "unknown"} | '
                f'item={item["desc"]} | hint={self._ai_hint_text(item["desc"])}'
            )
        prompt = (
            f'Classify each {source_label} into exactly one category.\n'
            f'Allowed categories: {", ".join(CATEGORY_ORDER)}.\n'
            'Abbreviation hints: CKN/CHKN=chicken, BNLS=boneless, YGRT=yogurt, '
            'BTR=butter, SSG=sausage, FLNGS=fillets, BCN=bacon.\n'
            'Return JSON object with key "results". Include exactly one result per input id.\n'
            '\n'.join(lines)
        )
        for attempt in range(2):
            started = time.perf_counter()
            try:
                self.stats['calls'] += 1
                self.stats['batch_items'] += len(items)
                resp = client.responses.create(
                    model='gpt-4o-mini', input=prompt, temperature=0,
                    max_output_tokens=max(200, 35 * len(items)),
                    text={'format': {'type': 'json_schema', 'name': 'receipt_item_categories',
                                     'strict': True, 'schema': schema}},
                )
                elapsed = time.perf_counter() - started
                self._record_call_metrics(elapsed, len(items))
                text = (resp.output_text or '').strip()
                if not text:
                    self.stats['no_result'] += len(items)
                    return {}
                payload = json.loads(text)
                raw_results = payload.get('results', [])
                results_by_id: Dict[str, Optional[str]] = {item['id']: None for item in items}
                for rec in raw_results:
                    rec_id = str(rec.get('id', ''))
                    category = canonicalize_category(rec.get('category'))
                    confidence = float(rec.get('confidence', 0.0))
                    if rec_id in results_by_id and category in CATEGORY_ORDER and confidence >= self.confidence_threshold:
                        results_by_id[rec_id] = category
                self.consecutive_errors = 0
                self.stats['success'] += sum(1 for v in results_by_id.values() if v)
                self.stats['no_result'] += sum(1 for v in results_by_id.values() if not v)
                return results_by_id
            except (APIConnectionError, APITimeoutError, RateLimitError) as e:
                elapsed = time.perf_counter() - started
                self._record_call_metrics(elapsed, len(items))
                if attempt == 0:
                    time.sleep(0.35)
                    continue
                self.stats['api_errors'] += 1
                self.consecutive_errors += 1
                if self.consecutive_errors >= self.max_consecutive_errors:
                    self.runtime_disabled = True
                    print('AI fallback disabled after repeated API errors '
                          f'({self.consecutive_errors}): {type(e).__name__}: {e}')
                self.stats['no_result'] += len(items)
                return {}
            except AuthenticationError as e:
                self.stats['api_errors'] += 1
                self.runtime_disabled = True
                print(f'AI fallback disabled due to auth error: {type(e).__name__}: {e}')
                self.stats['no_result'] += len(items)
                return {}
            except Exception:
                self.stats['no_result'] += len(items)
                return {}
        self.stats['no_result'] += len(items)
        return {}

    def try_single_category(self, desc: str, sku: Optional[str] = None,
                            source_label: str = 'item') -> Optional[str]:
        cached = self.get_cached_category(desc)
        if cached:
            return cached
        results = self.try_category_batch(
            [{'id': 'single', 'sku': sku or '', 'desc': desc}], source_label=source_label
        )
        category = results.get('single')
        self.set_cached_category(desc, category)
        return category
