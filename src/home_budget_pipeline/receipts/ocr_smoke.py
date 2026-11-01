"""Require real Paddle detection/recognition, including in the production build."""
import json
import os
import time

from PIL import Image, ImageDraw, ImageFont
from .ingest import _run_paddle_candidate, paddle_mkldnn_enabled


def main():
    # Synthetic text only: no receipt files, database, cache or broker operations.
    os.environ['HOME_BUDGET_PADDLE_OCR'] = 'true'
    image = Image.new('RGB', (900, 350), 'white')
    ImageDraw.Draw(image).text(
        (30, 30), 'BROWNROOK OCR CHECK\nTOTAL 12.34',
        fill='black', font=ImageFont.load_default(size=40),
    )
    started = time.monotonic()
    candidate = _run_paddle_candidate(image, dpi=200)
    recognized = candidate.status == 'success' and all(
        word in candidate.text for word in ('BROWNROOK', '12.34')
    )
    print(json.dumps({'event': 'paddle_runtime_check',
        'status': 'success' if recognized else 'failed',
        'error_type': candidate.error_type, 'seconds': round(time.monotonic()-started, 3),
        'enable_mkldnn': paddle_mkldnn_enabled(), 'recognized_lines': len(candidate.lines)}))
    return 0 if recognized else 1


if __name__ == '__main__':
    raise SystemExit(main())
