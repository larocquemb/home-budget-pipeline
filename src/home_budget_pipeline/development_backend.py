"""KAN-137: explicit development targets and guarded schema operations."""
from pathlib import Path
from urllib.parse import unquote, urlsplit

HOSTS = {'arsene.brownrook.net', '192.168.2.201'}
DATABASES = {'home_budget_dev': 'home_budget_dev', 'home_budget_test': 'home_budget_test'}


def validate_database_url(value, *, test=False):
    """Reject production, arbitrary hosts, admin users and ambiguous defaults."""
    try:
        url = urlsplit(value)
        database = unquote(url.path.removeprefix('/'))
        expected = 'home_budget_test' if test else 'home_budget_dev'
        valid = (url.scheme in {'postgres', 'postgresql'} and url.hostname in HOSTS
                 and url.port == 15432 and database == expected
                 and unquote(url.username or '') == DATABASES[expected]
                 and bool(url.password) and not url.fragment and not url.query)
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('Use the dedicated Arsene development port 15432 and matching development/test database role')


def verify_database(conn, *, test=False):
    expected = 'home_budget_test' if test else 'home_budget_dev'
    row = conn.execute("SELECT current_database(),current_user,current_setting('home_budget.environment',true)").fetchone()
    if tuple(row) != (expected, DATABASES[expected], 'development'):
        raise ValueError('Connected database identity or development marker does not match; refusing schema changes')


def validate_endpoints(values):
    validate_database_url(values.get('DATABASE_URL', ''))
    if values.get('TEST_DATABASE_URL'):
        validate_database_url(values['TEST_DATABASE_URL'], test=True)
    for key, scheme, port, path, username in (
        ('RABBITMQ_URL', 'amqp', 15673, '/home-budget-dev', 'home_budget_dev'),
        ('TEST_RABBITMQ_URL', 'amqp', 15673, '/home-budget-test', 'home_budget_test'),
        ('NEO4J_URI', 'bolt', 17687, '', None),
    ):
        try:
            url = urlsplit(values.get(key, ''))
            valid = (url.scheme == scheme and url.hostname in HOSTS and url.port == port
                     and unquote(url.path) == path and not url.query and not url.fragment)
            if username:
                valid = valid and unquote(url.username or '') == username and bool(url.password)
            else:
                valid = valid and url.username is None and url.password is None
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(f'{key} must select the isolated Arsene development service')
    if values.get('HOME_BUDGET_ENVIRONMENT') != 'development':
        raise ValueError('HOME_BUDGET_ENVIRONMENT=development is required')


def bootstrap(conn, root: Path, *, test=False, reset=False, confirmation=''):
    """Only operate on a positively identified development database."""
    verify_database(conn, test=test)
    expected = 'home_budget_test' if test else 'home_budget_dev'
    existing = conn.execute("SELECT to_regnamespace('budget')").fetchone()[0]
    if reset:
        if confirmation != expected:
            raise ValueError('Reset requires --confirm with the exact development database name')
        conn.execute('''DROP SCHEMA IF EXISTS ingest_blue CASCADE;
            DROP SCHEMA IF EXISTS ingest_green CASCADE;
            DROP SCHEMA IF EXISTS ingest CASCADE;
            DROP SCHEMA IF EXISTS ops CASCADE;
            DROP SCHEMA IF EXISTS enrichment CASCADE;
            DROP SCHEMA IF EXISTS lineage CASCADE;
            DROP SCHEMA IF EXISTS budget CASCADE;''')
    elif existing:
        raise ValueError('Database is already initialized; use migrations or an explicitly confirmed development reset')
    for path in sorted(root.glob('*.sql')):
        conn.execute(path.read_text())
