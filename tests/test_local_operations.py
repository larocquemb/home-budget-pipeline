"""Local maintenance must preserve evidence and reject remote targets."""
import hashlib
import json
import os
from argparse import Namespace

import pytest

from home_budget_pipeline import cli, local_operations as ops


def local_values(root):
    return {'DATABASE_URL': 'postgresql://home_budget_dev:secret@arsene.brownrook.net:15432/home_budget_dev',
            'RABBITMQ_URL': 'amqp://home_budget_dev:secret@arsene.brownrook.net:15673/home-budget-dev',
            'HOME_BUDGET_OCR_CACHE': str(root / '.ocr_cache')}


@pytest.mark.parametrize('key,value', [
    ('DATABASE_URL', 'postgresql://paul@arsene.brownrook.net:5432/home_budget'),
    ('DATABASE_URL', 'postgresql://paul@localhost:5432/production'),
    ('DATABASE_URL', 'postgresql://paul@localhost/home_budget?host=production'),
    ('RABBITMQ_URL', 'amqp://arsene.brownrook.net/'),
    ('HOME_BUDGET_OCR_CACHE', '/Volumes/files/ocr-cache'),
])
def test_local_maintenance_refuses_wrong_targets(tmp_path, key, value):
    values = local_values(tmp_path)
    values[key] = value
    with pytest.raises(ValueError):
        ops.validate_local(values, tmp_path)


def test_symlink_cache_is_rejected_without_touching_target(tmp_path):
    target = tmp_path / 'original'
    target.mkdir()
    (tmp_path / '.ocr_cache').symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError):
        ops.validate_local(local_values(tmp_path), tmp_path)
    assert target.is_dir()


def test_clean_retains_original_outputs_timestamps_and_hashes(tmp_path):
    cache = tmp_path / '.ocr_cache'
    cache.mkdir()
    original = cache / 'receipt.json'
    original.write_bytes(b'original OCR evidence')
    timestamp = original.stat().st_mtime_ns
    ops.clean_cache(cache, tmp_path)
    assert list(cache.iterdir()) == []
    manifest_path, = (tmp_path / '.validation/cache-backups').glob('*/manifest.private.json')
    manifest = json.loads(manifest_path.read_text())
    retained = manifest_path.parent / 'cache/receipt.json'
    assert retained.read_bytes() == b'original OCR evidence'
    assert retained.stat().st_mtime_ns == timestamp
    assert manifest['artifacts'][0]['sha256'] == hashlib.sha256(retained.read_bytes()).hexdigest()
    assert manifest_path.stat().st_mode & 0o777 == 0o600


def test_explicit_environment_supplies_defaults_without_leaking_into_next_command(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_URL', 'previous-database')
    env = tmp_path / '.env.dev'
    env.write_text('DATABASE_URL=postgresql://paul@localhost/home_budget\nHOME_BUDGET_OCR_CACHE=/tmp/local-ocr\n')
    observed = []
    monkeypatch.setattr(cli, '_setup_database', lambda args: observed.append(args.db_dsn) or 0)
    assert cli.main(['--env-file', str(env), 'database', 'setup']) == 0
    assert observed == ['postgresql://paul@localhost/home_budget']
    assert os.environ['DATABASE_URL'] == 'previous-database'
    assert os.environ.get('HOME_BUDGET_OCR_CACHE') != '/tmp/local-ocr'


def test_wrong_environment_refused_before_services_stop(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, 'ROOT', tmp_path)
    (tmp_path / '.env.dev').write_text('DATABASE_URL=postgresql://production/home_budget\n')
    monkeypatch.setattr(ops, '_stop_services', lambda: pytest.fail('stopped services before validating'))
    with pytest.raises(ValueError):
        ops.clean_local_cache(Namespace(env_file=None))


def test_arsene_rebuild_dispatch_is_explicit_and_preserves_confirmation(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(ops.subprocess, 'run', lambda command, **kw: calls.append(command) or Namespace(returncode=0))
    args = Namespace(target='arsene-test', env_file=str(tmp_path / '.env'), confirm='home_budget_test')
    assert ops.rebuild_database(args) == 0
    assert calls[0][-1] == '--test'
    assert calls[0][calls[0].index('--confirm') + 1] == 'home_budget_test'



def test_database_rebuild_has_no_native_mac_or_production_target():
    for target in ('local', 'production'):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(['database', 'rebuild', '--target', target])
