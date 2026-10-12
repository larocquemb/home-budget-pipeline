"""Readable product labels, separate from immutable retailer and OCR evidence."""
import re
from urllib.parse import urlparse

from .product_enrichment import RETAILER_DOMAINS


def product_name(title: str | None, url: str | None = None) -> str:
    """Remove leading shopping prompts and a matching retailer's title suffix."""
    name = (title or '').strip()
    name = re.sub(r'^(?:(?:buy|shop|purchase|order)(?:\s+now)?\s+)+', '', name, flags=re.I)
    try:
        hostname = (urlparse(url or '').hostname or '').lower()
    except ValueError:
        hostname = ''
    head, separator, suffix = name.rpartition('|')
    if separator:
        normalized = re.sub(r'[^a-z0-9]+', ' ', suffix.lower()).strip()
        normalized = re.sub(r'\s+(?:inc|ltd|limited|incorporated|corporation|corp)$', '', normalized)
        aliases = {retailer for retailer, domain in RETAILER_DOMAINS.items()
                   if hostname == domain or hostname.endswith('.' + domain)}
        if normalized in aliases or suffix.strip().lower() == hostname.removeprefix('www.'):
            name = head.strip()
    return name


def corrected_reading(original: str, evidence: dict | None) -> str | None:
    """Show only explicitly scored one-glyph brand corrections, never new OCR."""
    match = re.match(r'(\s*)(\S+)(.*)', original or '', flags=re.S)
    if not match:
        return None
    token = match[2]
    for entry in (evidence or {}).get('tokens', []):
        replacement = entry.get('matched') or ''
        if (entry.get('kind') == 'ocr_brand_initialism' and entry.get('token') == token.lower()
                and re.fullmatch(r'[a-zA-Z]{2,5}', replacement)
                and len(replacement) == len(token)
                and sum(a != b for a, b in zip(token.lower(), replacement.lower())) == 1):
            return match[1] + replacement.capitalize() + match[3]
    return None


def ocr_variants(payload: dict) -> list[dict]:
    """Group related OCR observations; text retrieval does not prove item identity."""
    sources = {s['id']: s for s in (payload.get('evidence_bundle') or {}).get('sources', [])
               if s.get('id') and s.get('kind') in {'ocr_pass', 'ocr_consensus'}}
    grouped, seen = {}, set()
    for hypothesis in payload.get('reading_hypotheses', []):
        ids = [key for key in hypothesis.get('source_ids', []) if key in sources and key not in seen]
        if ids and hypothesis.get('reading'):
            grouped.setdefault(hypothesis['reading'], []).extend(sources[key] for key in ids)
            seen.update(ids)
    for key, source in sources.items():
        if key not in seen:
            grouped.setdefault(source.get('text') or '', []).append(source)
    fields = ['id', 'kind', 'text', 'engine', 'pass_id', 'variant', 'ocr_run_uuid', 'line_number', 'page_number', 'evidence_id']
    return [{'reading': reading, 'observations': [{**{key: source.get(key) for key in fields},
                'row_association': 'unverified', 'association_method': 'text_similarity'}
            for source in observations]} for reading, observations in grouped.items() if reading]
