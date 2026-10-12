#!/usr/bin/env python3
"""Load selected local pipeline settings without executing an environment file."""
from pathlib import Path
import os
import sys
from postgres_credentials import read_env
from home_budget_pipeline.enrichment_evaluation import main


def configure_environment(path):
    if path.is_file():
        values = read_env(path)
        for key in ('BRAVE_SEARCH_API_KEY', 'OLLAMA_BASE_URL', 'OLLAMA_HOST', 'OLLAMA_CONTEXT_TOKENS',
                    'COMPARISON_TIMEOUT_SECONDS', 'BRAVE_TIMEOUT_SECONDS', 'DATABASE_URL', 'HOME_BUDGET_PG_DSN',
                    'HOME_BUDGET_DATA_ROOT', 'HOME_BUDGET_SCANNED_INBOX', 'HOME_BUDGET_RECEIPTS_ROOT',
                    'HOME_BUDGET_OCR_CACHE', 'QWEN_CONTEXT_TOKENS', 'QWEN_OUTPUT_TOKENS'):
            if values.get(key) and not os.getenv(key):
                os.environ[key] = values[key]


if __name__ == '__main__':
    arguments = sys.argv[1:]
    if '--env-file' in arguments:
        index = arguments.index('--env-file')
        if index + 1 >= len(arguments):
            raise SystemExit('--env-file requires a path')
        configure_environment(Path(arguments[index + 1]))
        del arguments[index:index + 2]
    raise SystemExit(main(arguments))
