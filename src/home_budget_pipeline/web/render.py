"""Small dependency-free HTML renderer for the BrownRook Ledger."""

from __future__ import annotations

import html
from decimal import Decimal
from typing import Any, Iterable, Mapping
from urllib.parse import urlencode


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def money(value: Any) -> str:
    if value is None or value == "":
        return ""
    try:
        return f"${Decimal(str(value)):,.2f}"
    except Exception:
        return esc(value)


def page(title: str, body: str, *, base_path: str, identity: Mapping[str, str]) -> str:
    who = esc(identity.get("email") or identity.get("user") or "")
    nav = (
        f'<a href="{base_path}">Home</a>'
        f'<a href="{base_path}/expenses">Expenses</a>'
        f'<a href="{base_path}/category-spend">Category spend</a>'
        f'<a href="{base_path}/review-queue">Review queue</a>'
        f'<a href="{base_path}/duplicates">Duplicates</a>'
        f'<a href="{base_path}/transactions">Transactions</a>'
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · BrownRook Ledger</title>
<style>
:root {{ color-scheme: light dark; font-family: system-ui, -apple-system, sans-serif; }}
body {{ margin: 0; }}
header, main {{ max-width: 92rem; margin: auto; padding: 1rem 1.5rem; }}
header {{ border-bottom: 1px solid #8885; }}
nav {{ display:flex; flex-wrap:wrap; gap:1rem; margin:.75rem 0; }}
a {{ color: inherit; }}
table {{ width:100%; border-collapse:collapse; font-size:.92rem; }}
th, td {{ padding:.55rem .65rem; border-bottom:1px solid #8884; text-align:left; vertical-align:top; }}
th {{ position:sticky; top:0; background:Canvas; }}
.num {{ text-align:right; white-space:nowrap; }}
.review {{ font-weight:700; }}
.muted {{ opacity:.7; }}
.toolbar {{ display:flex; flex-wrap:wrap; gap:.75rem; align-items:end; margin:1rem 0; }}
.toolbar label {{ display:grid; gap:.25rem; }}
input, select, button {{ font:inherit; padding:.4rem .5rem; }}
.card {{ border:1px solid #8885; border-radius:.5rem; padding:1rem; margin:1rem 0; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(15rem,1fr)); gap:.75rem 1.5rem; }}
.receipt-review-grid {{ display:grid; grid-template-columns:minmax(0,1fr) minmax(22rem,.8fr); gap:1.5rem; align-items:start; }}
.receipt-preview-viewport {{ position:relative; overflow:hidden; max-height:75vh; border-radius:.5rem; }}
.receipt-preview {{ display:block; width:100%; border:1px solid #8885; border-radius:.5rem; background:#fff; }}
.receipt-preview-image {{ width:auto; max-width:100%; max-height:75vh; margin:auto; object-fit:contain; transform-origin:var(--zoom-x,50%) var(--zoom-y,50%); transition:transform .2s ease-out; }}
.receipt-preview-viewport:hover .receipt-preview-image {{ cursor:zoom-in; transform:scale(1.5); }}
.receipt-text {{ box-sizing:border-box; max-height:75vh; margin:0; padding:1rem; overflow:auto; border:1px solid #8885; border-radius:.5rem; }}
pre {{ white-space:pre-wrap; overflow-wrap:anywhere; }}
.pager {{ display:flex; gap:1rem; margin:1rem 0; }}
@media (max-width: 60rem) {{ .receipt-review-grid {{ grid-template-columns:1fr; }} }}
</style>
</head>
<body>
<header><strong>BrownRook Ledger</strong><nav>{nav}</nav><small class="muted">Signed in as {who}</small></header>
<main><h1>{esc(title)}</h1>{body}</main>
<script>
for (const image of document.querySelectorAll('[data-hover-zoom]')) {{
  const viewport = image.closest('.receipt-preview-viewport');
  let animationFrame = null;
  let pointerX = 0;
  let pointerY = 0;
  viewport.addEventListener('pointermove', event => {{
    pointerX = event.clientX;
    pointerY = event.clientY;
    if (animationFrame !== null) return;
    animationFrame = requestAnimationFrame(() => {{
      const viewportBounds = viewport.getBoundingClientRect();
      const imageLeft = viewportBounds.left + image.offsetLeft;
      const imageTop = viewportBounds.top + image.offsetTop;
      const x = Math.max(0, Math.min(100, (pointerX - imageLeft) / image.offsetWidth * 100));
      const y = Math.max(0, Math.min(100, (pointerY - imageTop) / image.offsetHeight * 100));
      image.style.setProperty('--zoom-x', `${{x}}%`);
      image.style.setProperty('--zoom-y', `${{y}}%`);
      animationFrame = null;
    }});
  }});
}}
</script>
</body></html>"""


def table(rows: Iterable[Mapping[str, Any]], columns: tuple[tuple[str, str], ...], *, links: Mapping[str, str] | None = None, money_columns: set[str] | None = None) -> str:
    rows = tuple(rows)
    links = links or {}
    money_columns = money_columns or set()
    head = "".join(f"<th>{esc(label)}</th>" for _, label in columns)
    rendered = []
    for row in rows:
        cells = []
        for key, _ in columns:
            value = row.get(key)
            text = money(value) if key in money_columns else esc(value)
            if key in links and value is not None:
                text = f'<a href="{links[key].format(value=value)}">{text}</a>'
            cls = ' class="num"' if key in money_columns else ""
            cells.append(f"<td{cls}>{text}</td>")
        rendered.append("<tr>" + "".join(cells) + "</tr>")
    if not rendered:
        rendered.append(f'<tr><td colspan="{len(columns)}" class="muted">No rows found.</td></tr>')
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(rendered)}</tbody></table>"


def pager(path: str, *, limit: int, offset: int, row_count: int, query: Mapping[str, Any] | None = None, total_count: int | None = None) -> str:
    query = {k: v for k, v in (query or {}).items() if v not in (None, "")}
    items = []
    if offset > 0:
        prev = dict(query, limit=limit, offset=max(0, offset - limit))
        items.append(f'<a href="{path}?{urlencode(prev)}">← Previous</a>')
    if (total_count is not None and offset + row_count < total_count) or (total_count is None and row_count == limit):
        nxt = dict(query, limit=limit, offset=offset + limit)
        items.append(f'<a href="{path}?{urlencode(nxt)}">Next →</a>')
    if total_count is not None:
        page_number = offset // limit + 1
        page_count = max(1, (total_count + limit - 1) // limit)
        summary = f'<span class="muted">Page {page_number} of {page_count}</span>'
        items.insert(1 if offset > 0 else 0, summary)
    return '<div class="pager">' + "".join(items) + "</div>"
