"""Prepare K3s receipt reporting and TLS; never restart PostgreSQL automatically."""
from __future__ import annotations
import argparse
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import subprocess

from psycopg import sql
from postgres_credentials import Cluster, ROOT, read_env

ROLE = 'grafana_receipt_reader'


def scram_verifier(password: str, salt: bytes) -> str:
    salted = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 4096)
    client = hmac.new(salted, b'Client Key', hashlib.sha256).digest()
    stored = hashlib.sha256(client).digest()
    server = hmac.new(salted, b'Server Key', hashlib.sha256).digest()
    enc = lambda value: base64.b64encode(value).decode()
    return f'SCRAM-SHA-256$4096:{enc(salt)}${enc(stored)}:{enc(server)}'


def reader_sql(report: str, verifier: str) -> str:
    # SQL literals are encoded by psycopg; no shell interpolation of secrets.
    role = sql.Identifier(ROLE)
    alter = sql.SQL('ALTER ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
                    'NOREPLICATION NOBYPASSRLS PASSWORD {}').format(role, sql.Literal(verifier)).as_string()
    return f"""BEGIN;
DO $$ BEGIN
 IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{ROLE}' AND
    (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls))
 OR EXISTS (SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member WHERE r.rolname='{ROLE}')
 THEN RAISE EXCEPTION 'Reader has elevated privileges'; END IF;
 IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{ROLE}') THEN CREATE ROLE {ROLE} LOGIN; END IF;
END $$;
{alter};
ALTER ROLE {ROLE} SET default_transaction_read_only=on;
CREATE OR REPLACE VIEW lineage.receipt_processing_attempts WITH (security_barrier=true) AS
{report.rstrip().rstrip(';')};
GRANT CONNECT ON DATABASE home_budget TO {ROLE};
GRANT USAGE ON SCHEMA lineage TO {ROLE};
GRANT SELECT ON lineage.receipt_processing_attempts TO {ROLE};
DO $$ BEGIN
 IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE left(n.nspname,3)<>'pg_' AND n.nspname<>'information_schema'
 AND c.relkind IN ('r','p','v','m','f') AND c.oid<>'lineage.receipt_processing_attempts'::regclass
 AND has_table_privilege('{ROLE}',c.oid,'SELECT,INSERT,UPDATE,DELETE'))
 THEN RAISE EXCEPTION 'Reader can access other application tables'; END IF;
END $$;
COMMIT;
"""


# PostgreSQL flags a valid hostssl rule as inactive while TLS is still off.
# Preparation precedes the GitOps rollout that enables TLS, so allow only that
# exact warning on our reader rule. All other HBA errors remain fatal.
HBA_ERROR_QUERY = """SELECT count(*) FROM pg_hba_file_rules
WHERE error IS NOT NULL AND NOT (
    current_setting('ssl') = 'off'
    AND type = 'hostssl'
    AND user_name = ARRAY['grafana_receipt_reader']::text[]
    AND database = ARRAY['home_budget']::text[]
    AND error = 'hostssl record cannot match because SSL is disabled'
)"""


def setup_step(cluster, label, *args, payload=None):
    try:
        return cluster.run(*args, payload=payload)
    except Exception:
        raise RuntimeError(f'{label} failed; command output withheld to protect credentials.') from None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    state = ROOT / '.local-services/postgres-k3s'
    pki = Path.home() / 'brownrook-ca'
    tls = pki / 'leafs/k3s-postgres'
    cert, key = tls / 'server.crt', tls / 'server.key'
    root, intermediate = pki / 'root/root_ca.crt', pki / 'intermediate/intermediate_ca.crt'
    for path in (cert, key, root, intermediate):
        if not path.is_file():
            parser.exit(1, f'Missing {path}; sign the prepared CSR first.\n')
    if key.stat().st_mode & 0o077:
        parser.exit(1, 'TLS private key must have mode 0600.\n')
    subprocess.run(['openssl', 'verify', '-purpose', 'sslserver', '-verify_hostname', 'postgres.brownrook.net',
                    '-CAfile', str(root), '-untrusted', str(intermediate), str(cert)], check=True)
    subprocess.run(['openssl', 'x509', '-in', str(cert), '-checkend', '86400', '-noout'], check=True)
    if subprocess.check_output(['openssl', 'pkey', '-in', str(key), '-pubout']) != subprocess.check_output([
            'openssl', 'x509', '-in', str(cert), '-pubkey', '-noout']):
        parser.exit(1, 'TLS certificate does not match its private key.\n')
    values = read_env(ROOT / '.env.k3s')
    if values.get('KUBE_CONTEXT') != 'brownrook-k3s1' or values.get('KUBE_NAMESPACE') != 'home-budget':
        parser.exit(1, 'Expected brownrook-k3s1/home-budget in .env.k3s.\n')
    cluster = Cluster(values)
    # Database identity checked using the pod's existing admin credentials, never printed.
    identity = setup_step(cluster, 'Database identity check', 'exec', 'postgres-0', '-c', 'postgres', '--', 'sh', '-c',
                           'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT current_database()"').strip()
    if identity != 'home_budget':
        parser.exit(1, 'Unexpected production database.\n')
    if not args.apply:
        print('K3s identity and TLS inputs verified; no cluster changes.')
        return
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    state.chmod(0o700)
    credential_file = state / 'grafana-reader.password'
    if not credential_file.exists():
        with os.fdopen(os.open(credential_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as stream:
            stream.write(secrets.token_hex(32))
    if credential_file.stat().st_mode & 0o077:
        parser.exit(1, 'Reader password file must have mode 0600.\n')
    password = credential_file.read_text().strip()
    if not password or not password.isascii():
        parser.exit(1, 'Reader password must be nonempty ASCII.\n')
    statement = reader_sql((ROOT / 'sql/reports/receipt_processing_timeline.sql').read_text(),
                           scram_verifier(password, secrets.token_bytes(16)))
    setup_step(cluster, 'Reader authentication rule installation', 'exec', '-i', 'postgres-0', '-c', 'postgres', '--', 'sh', '-s', payload=r"""
set -eu
hba=$(psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc 'SHOW hba_file')
tmp=$(mktemp "${hba}.reporting.XXXXXX")
trap 'rm -f "$tmp"' EXIT
{
cat <<'HBA'
# BEGIN home-budget Grafana receipt reader
hostssl home_budget grafana_receipt_reader all scram-sha-256
host all grafana_receipt_reader all reject
local all grafana_receipt_reader reject
# END home-budget Grafana receipt reader
HBA
sed '/^# BEGIN home-budget Grafana receipt reader$/,/^# END home-budget Grafana receipt reader$/d' "$hba"
} > "$tmp"
chown --reference="$hba" "$tmp"
chmod --reference="$hba" "$tmp"
mv "$tmp" "$hba"
psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -c 'SELECT pg_reload_conf()'

""")
    hba_errors = setup_step(cluster, 'Authentication rule validation',
                            'exec', '-i', 'postgres-0', '-c', 'postgres', '--', 'sh', '-c',
                            'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -v ON_ERROR_STOP=1',
                            payload=HBA_ERROR_QUERY).strip()
    if hba_errors != '0':
        raise RuntimeError('Authentication rules contain unexpected errors; review pg_hba_file_rules.')
    setup_step(cluster, 'Report view and reader transaction', 'exec', '-i', 'postgres-0', '-c', 'postgres', '--', 'sh', '-c',
                'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1', payload=statement)
    resource = {'apiVersion': 'v1', 'kind': 'Secret', 'type': 'Opaque',
                'metadata': {'name': 'postgres-reporting-tls', 'namespace': 'home-budget'},
                'data': {'server.crt': base64.b64encode(cert.read_bytes() + intermediate.read_bytes()).decode(),
                         'server.key': base64.b64encode(key.read_bytes()).decode()}}
    current = setup_step(cluster, 'TLS Secret lookup', 'get', 'secret', 'postgres-reporting-tls', '--ignore-not-found', '-o', 'json')
    if current.strip():
        existing = json.loads(current)
        existing['data'] = resource['data']
        existing.get('metadata', {}).get('annotations', {}).pop('kubectl.kubernetes.io/last-applied-configuration', None)
        setup_step(cluster, 'TLS Secret update', 'replace', '-f', '-', payload=json.dumps(existing))
    else:
        setup_step(cluster, 'TLS Secret creation', 'create', '-f', '-', payload=json.dumps(resource))
    print('Created restricted report view, reader and TLS Secret. No PostgreSQL restart requested.')
    print('Deploy the PostgreSQL TLS GitOps patch, then run make monitoring-receipt-postgres-both-apply.')


if __name__ == '__main__':
    try:
        main()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from None
    except Exception:
        raise SystemExit('K3s reporting setup failed; details withheld to protect credentials. Check TLS inputs and cluster access.') from None
