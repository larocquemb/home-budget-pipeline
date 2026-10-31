"""Explicit environment-file loading for Ledger command defaults."""
from contextlib import contextmanager
import os
from pathlib import Path
import re
import shlex
from urllib.parse import quote


def read_environment(path):
    values = {}
    for number, line in enumerate(Path(path).read_text().splitlines(), 1):
        try:
            tokens = shlex.split(line, comments=True)
        except ValueError:
            raise ValueError(f'Invalid environment-file quoting at line {number}') from None
        if tokens and tokens[0] == 'export':
            tokens.pop(0)
        if not tokens:
            continue
        if len(tokens) != 1 or '=' not in tokens[0]:
            raise ValueError(f'Expected KEY=value at environment-file line {number}')
        key, value = tokens[0].split('=', 1)
        if not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', key):
            raise ValueError(f'Invalid variable name at environment-file line {number}')
        values[key] = value
    for key in ('DATABASE_URL', 'HOME_BUDGET_PG_DSN'):
        if key not in values:
            continue

        def substitute(match):
            name = match[1]
            allowed = {'POSTGRES_USER', 'POSTGRES_PASSWORD', 'POSTGRES_DB', 'POSTGRES_HOST', 'POSTGRES_PORT'}
            if key == 'HOME_BUDGET_PG_DSN':
                allowed.add('DATABASE_URL')
            if name not in allowed or not values.get(name):
                raise ValueError(f'Unsupported or unset variable referenced by {key}')
            return quote(values[name], safe='') if name in {'POSTGRES_USER', 'POSTGRES_PASSWORD', 'POSTGRES_DB'} else values[name]

        values[key] = re.sub(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}', substitute, values[key])
    if values.get('DATABASE_URL'):
        values['HOME_BUDGET_PG_DSN'] = values['DATABASE_URL']
    return values


@contextmanager
def environment_file(path):
    values = read_environment(path) if path else {}
    previous = {key: os.environ.get(key) for key in values}
    try:
        os.environ.update(values)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
