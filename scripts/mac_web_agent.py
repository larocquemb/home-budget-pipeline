#!/usr/bin/env python3
"""Run the local web application inside the signed macOS agent executable."""
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


def main():
    from home_budget_pipeline.cli_environment import read_environment
    from home_budget_pipeline.development_backend import validate_endpoints
    values = read_environment(ROOT / '.env.dev')
    validate_endpoints(values)
    if not values.get('LEDGER_PROXY_SECRET'):
        raise ValueError('Set LEDGER_PROXY_SECRET in .env.dev')
    os.environ.update(values)
    os.environ['LEDGER_BASE_PATH'] = '/ledger'
    import uvicorn
    uvicorn.run('home_budget_pipeline.web.receipt_app:app',
                host=os.environ.get('HOME_BUDGET_LOCAL_WEB_HOST', '127.0.0.1'),
                port=8080, reload=True, reload_dirs=[str(ROOT / 'src')])


if __name__ == '__main__':
    main()
