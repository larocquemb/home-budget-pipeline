"""Provision an isolated Arsene accounting database without touching receipts."""
import argparse
from pathlib import Path
import secrets
import sys
from urllib.parse import quote

import psycopg
from psycopg import sql
import yaml
from development_backend import private_write, update_env

ROOT = Path(__file__).resolve().parents[1]
DATABASE = 'home_budget_accounting_dev'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    if not args.apply:
        print('Provision home_budget_accounting_dev and dedicated role on Arsene:15432; preserve receipt database.')
        return
    provisioned=ROOT/'.dev-backend/accounting-secrets.yml'
    if provisioned.exists():
        password=yaml.safe_load(provisioned.read_text())['password']
    else:
        password=secrets.token_hex(32)
        private_write(provisioned,yaml.safe_dump({'password':password}),exclusive=True)
    admin=yaml.safe_load((ROOT/'.dev-backend/secrets.yml').read_text())['dev_backend_secrets']['postgres_admin']
    with psycopg.connect(host='arsene.brownrook.net',port=15432,dbname='postgres',user='development_admin',
                          password=admin,autocommit=True,connect_timeout=5) as conn:
        if not conn.execute('SELECT 1 FROM pg_roles WHERE rolname=%s',(DATABASE,)).fetchone():
            conn.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {}').format(sql.Identifier(DATABASE),sql.Literal(password)))
        if not conn.execute('SELECT 1 FROM pg_database WHERE datname=%s',(DATABASE,)).fetchone():
            conn.execute(sql.SQL('CREATE DATABASE {} OWNER {}').format(sql.Identifier(DATABASE),sql.Identifier(DATABASE)))
        owner=conn.execute('SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=%s',(DATABASE,)).fetchone()[0]
        if owner != DATABASE:
            raise ValueError('Accounting database has an unexpected owner; no client settings changed')
        conn.execute(sql.SQL('REVOKE ALL ON DATABASE {} FROM PUBLIC').format(sql.Identifier(DATABASE)))
        conn.execute(sql.SQL("ALTER DATABASE {} SET home_budget.environment='development'").format(sql.Identifier(DATABASE)))
    url=f'postgresql://{DATABASE}:{quote(password,safe="")}@arsene.brownrook.net:15432/{DATABASE}'
    with psycopg.connect(url) as conn:
        conn.execute('REVOKE CREATE ON SCHEMA public FROM PUBLIC')
        conn.execute((ROOT/'sql/accounting.sql').read_text())
    env=ROOT/'.env.dev'
    backup=ROOT/'.dev-backend/env-before-accounting.private'
    if not backup.exists():
        private_write(backup,env.read_text(),exclusive=True)
    private_write(env,update_env(env.read_text(),{'ACCOUNTING_DATABASE_URL':url}))
    print('Dedicated accounting database and schema ready; ACCOUNTING_DATABASE_URL configured. No receipt data changed.')


if __name__=='__main__':
    try:
        main()
    except Exception as exc:
        print(f'Accounting provisioning failed ({type(exc).__name__}); credentials suppressed',file=sys.stderr)
        raise SystemExit(2) from None
