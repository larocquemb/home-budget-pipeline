"""Configure native development PostgreSQL for the monitoring reader.

Check is read-only. Apply changes settings and HBA, but leaves the restart to
the operator so active OCR work need not be interrupted unexpectedly.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import platform
import secrets
import subprocess
import tempfile
from urllib.parse import urlsplit

import psycopg
from psycopg import sql

from postgres_credentials import read_env


ROOT = Path(__file__).resolve().parents[1]
ROLE = 'grafana_receipt_reader'
BEGIN = '# BEGIN home-budget Grafana receipt reader'
END = '# END home-budget Grafana receipt reader'


def reader_hba(contents: str) -> str:
    if BEGIN in contents or END in contents:
        if contents.count(BEGIN) != 1 or contents.count(END) != 1:
            raise ValueError('Invalid managed reader block in pg_hba.conf')
        start, rest = contents.split(BEGIN, 1)
        _, finish = rest.split(END, 1)
        contents = start + finish.lstrip('\n')
    block = (
        f'{BEGIN}\n'
        f'hostssl home_budget {ROLE} 192.168.2.202/32 scram-sha-256\n'
        f'host all {ROLE} 0.0.0.0/0 reject\n'
        f'host all {ROLE} ::/0 reject\n'
        f'{END}\n'
    )
    return block + contents


def native_dsn(dsn: str) -> str:
    address = urlsplit(dsn)
    if (address.scheme not in ('postgres', 'postgresql')
            or address.hostname not in ('localhost', '127.0.0.1', '::1')
            or address.port not in (None, 5432)
            or address.path != '/home_budget' or address.query):
        raise ValueError('Expected native localhost:5432/home_budget URL without query overrides')
    return dsn


def run() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--pki-dir', type=Path, default=Path.home() / 'brownrook-ca')
    args = parser.parse_args()
    if platform.system() != 'Darwin':
        raise RuntimeError('This command is for native Homebrew PostgreSQL on m4pro')
    settings = read_env(ROOT / '.env.dev')
    dsn = native_dsn(settings['DATABASE_URL'])
    pki = args.pki_dir.expanduser().resolve()
    identity = pki / 'leafs/m4pro-postgres'
    cert = identity / 'm4pro-postgres.crt'
    chain = identity / 'm4pro-postgres.fullchain.crt'
    key = identity / 'server.key'
    for path in (cert, chain, key, pki / 'root/root_ca.crt', pki / 'intermediate/intermediate_ca.crt'):
        if not path.is_file():
            raise RuntimeError(f'Missing TLS input: {path}')
    if key.stat().st_mode & 0o077:
        raise RuntimeError('server.key must be protected: chmod 600 server.key')
    subprocess.run([
        'openssl', 'verify', '-purpose', 'sslserver', '-verify_hostname', 'm4pro.local',
        '-CAfile', str(pki / 'root/root_ca.crt'),
        '-untrusted', str(pki / 'intermediate/intermediate_ca.crt'), str(cert),
    ], check=True)
    subprocess.run(['openssl', 'x509', '-in', str(cert), '-checkend', '86400', '-noout'], check=True)
    cert_pub = subprocess.check_output(['openssl', 'x509', '-in', str(cert), '-pubkey', '-noout'])
    key_pub = subprocess.check_output(['openssl', 'pkey', '-in', str(key), '-pubout'])
    if cert_pub != key_pub:
        raise RuntimeError('Certificate does not match server.key')
    if chain.read_bytes() != cert.read_bytes() + (pki / 'intermediate/intermediate_ca.crt').read_bytes():
        raise RuntimeError('Full chain must contain the leaf followed by the intermediate certificate')
    with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as conn:
        hba = Path(conn.execute('SHOW hba_file').fetchone()[0])
        data_dir = Path(conn.execute('SHOW data_directory').fetchone()[0])
        if data_dir != Path('/opt/homebrew/var/postgresql@18') or hba.parent != data_dir:
            raise RuntimeError('Expected native Homebrew PostgreSQL 18 data directory')
        if hba.stat().st_uid != os.getuid():
            raise RuntimeError('Run as the native PostgreSQL service owner')
        config = reader_hba(hba.read_text())
        existing = conn.execute('SELECT rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls '
                                'FROM pg_roles WHERE rolname = %s', (ROLE,)).fetchone()
        if existing and (any(existing) or conn.execute(
                'SELECT EXISTS (SELECT 1 FROM pg_auth_members WHERE member = '
                '(SELECT oid FROM pg_roles WHERE rolname = %s))', (ROLE,)).fetchone()[0]):
            raise RuntimeError('Existing Grafana reader has elevated privileges or role memberships')
        if not args.apply:
            print('Certificate, key, native database and reader policy checks passed.')
            print('Apply with: make dev-postgres-grafana-apply')
            return
        state = ROOT / '.local-services/postgres'
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        state.chmod(0o700)
        credential_file = state / 'grafana-reader.password'
        if not credential_file.exists():
            descriptor = os.open(credential_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'w') as handle:
                handle.write(secrets.token_hex(32))
        if credential_file.stat().st_mode & 0o077:
            raise RuntimeError('Reader password file must have mode 0600')
        password = credential_file.read_text().strip()
        if not password:
            raise RuntimeError('Reader password file is empty')
        if not existing:
            conn.execute(sql.SQL('CREATE ROLE {} LOGIN').format(sql.Identifier(ROLE)))
        conn.execute("SET password_encryption = 'scram-sha-256'")
        conn.execute(sql.SQL('ALTER ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
                             'NOREPLICATION NOBYPASSRLS PASSWORD {}').format(
                                 sql.Identifier(ROLE), sql.Literal(password)))
        conn.execute(sql.SQL('ALTER ROLE {} SET default_transaction_read_only = on').format(sql.Identifier(ROLE)))
        report = (ROOT / 'sql/reports/receipt_processing_timeline.sql').read_text().rstrip().rstrip(';')
        conn.execute('CREATE OR REPLACE VIEW lineage.receipt_processing_attempts '
                     'WITH (security_barrier=true) AS ' + report)
        conn.execute(sql.SQL('GRANT CONNECT ON DATABASE home_budget TO {}').format(sql.Identifier(ROLE)))
        conn.execute(sql.SQL('GRANT USAGE ON SCHEMA lineage TO {}').format(sql.Identifier(ROLE)))
        conn.execute(sql.SQL('GRANT SELECT ON lineage.receipt_processing_attempts TO {}').format(sql.Identifier(ROLE)))
        if conn.execute("SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                        "WHERE left(n.nspname,3) <> 'pg_' AND n.nspname <> 'information_schema' "
                        "AND c.relkind IN ('r','p','v','m','f') "
                        "AND c.oid <> 'lineage.receipt_processing_attempts'::regclass "
                        "AND has_table_privilege(%s,c.oid,'SELECT,INSERT,UPDATE,DELETE'))",
                        (ROLE,)).fetchone()[0]:
            raise RuntimeError('Reader can access other application tables; review existing grants before LAN access')
        with tempfile.NamedTemporaryFile(mode='w', dir=hba.parent, prefix='.receipt-hba-', delete=False) as handle:
            handle.write(config)
            replacement = Path(handle.name)
        replacement.chmod(hba.stat().st_mode & 0o777)
        replacement.replace(hba)
        for name, value in {
            'listen_addresses': 'localhost,0.0.0.0',
            'ssl': 'on', 'ssl_cert_file': str(chain), 'ssl_key_file': str(key),
            'ssl_min_protocol_version': 'TLSv1.2',
        }.items():
            conn.execute(sql.SQL('ALTER SYSTEM SET {} = {}').format(sql.Identifier(name), sql.Literal(value)))
        print('Configured TLS and the read-only receipt view; PostgreSQL restart is pending.')
        print(f'Grafana password file (keep private): {credential_file}')
        print('Grafana endpoint: m4pro.local:5432; database: home_budget; user: ' + ROLE)
        print('Next: brew services restart postgresql@18')


if __name__ == '__main__':
    run()
