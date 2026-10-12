import base64
import hashlib
import hmac
import importlib.util
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('k3s_reporting', ROOT / 'scripts/configure_k3s_receipt_postgres.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_scram_verifier_matches_password():
    password, salt = 'independent-reader-password', b'1234567890123456'
    result = module.scram_verifier(password, salt)
    assert result.startswith('SCRAM-SHA-256$4096:' + base64.b64encode(salt).decode() + '$')
    stored, server = result.split('$')[-1].split(':')
    salted = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 4096)
    assert base64.b64decode(stored) == hashlib.sha256(hmac.new(salted, b'Client Key', hashlib.sha256).digest()).digest()
    assert base64.b64decode(server) == hmac.new(salted, b'Server Key', hashlib.sha256).digest()


def test_reader_transaction_enforces_view_only_and_quotes_verifier():
    statement = module.reader_sql('SELECT 1 AS receipt;', "value'quoted")
    assert statement.startswith('BEGIN;')
    assert statement.rstrip().endswith('COMMIT;')
    assert "'value''quoted'" in statement
    assert 'NOBYPASSRLS' in statement
    assert 'pg_auth_members' in statement
    assert "has_table_privilege" in statement
    assert 'GRANT SELECT ON lineage.receipt_processing_attempts' in statement


def test_preparation_allows_only_expected_inactive_reader_tls_rule():
    query = module.HBA_ERROR_QUERY
    assert "current_setting('ssl') = 'off'" in query
    assert "type = 'hostssl'" in query
    assert "user_name = ARRAY['grafana_receipt_reader']" in query
    assert "database = ARRAY['home_budget']" in query
    assert "error = 'hostssl record cannot match because SSL is disabled'" in query


def test_step_error_identifies_operation_without_disclosing_secrets():
    import pytest

    class BrokenCluster:
        def run(self, *args, **kwargs):
            raise RuntimeError('sensitive-password-and-secret-payload')

    with pytest.raises(RuntimeError, match='TLS Secret creation failed') as error:
        module.setup_step(BrokenCluster(), 'TLS Secret creation', 'create', payload='secret')
    assert 'sensitive-password' not in str(error.value)
    assert 'secret-payload' not in str(error.value)
