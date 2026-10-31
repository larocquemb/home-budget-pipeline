"""Ensure the authentication probe cannot report fallback or infrastructure errors as success."""
import base64
import importlib.util
import json
from pathlib import Path
import struct
import time

import pytest


@pytest.fixture
def probe():
    path = Path(__file__).resolve().parents[1] / 'scripts/postgres_entra_probe.py'
    spec = importlib.util.spec_from_file_location('entra_probe', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def frame(kind, payload):
    return kind + struct.pack('!I', len(payload) + 4) + payload


def authentication(code, payload=b''):
    return frame(b'R', struct.pack('!I', code) + payload)


class Wire:
    def __init__(self, payload):
        self.payload = payload
        self.sent = []

    def recv(self, size):
        # Exercise fragmented reads, including message headers.
        size = min(size, 3)
        result, self.payload = self.payload[:size], self.payload[size:]
        return result

    def sendall(self, data):
        self.sent.append(data)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_actual_oauth_exchange_required(probe, monkeypatch):
    wire = Wire(authentication(0) + frame(b'Z', b'I'))
    monkeypatch.setattr(probe.socket, 'create_connection', lambda *a, **k: wire)
    with pytest.raises(RuntimeError, match='Unexpected authentication'):
        probe.database_login(1234, 'private-token')
    assert all(b'private-token' not in sent for sent in wire.sent)


def test_password_fallback_rejected(probe, monkeypatch):
    wire = Wire(authentication(10, b'SCRAM-SHA-256\0\0'))
    monkeypatch.setattr(probe.socket, 'create_connection', lambda *a, **k: wire)
    with pytest.raises(RuntimeError, match='did not require OAUTHBEARER'):
        probe.database_login(1234, 'private-token')


def test_valid_wire_login_and_role_query(probe, monkeypatch):
    identity = struct.pack('!H', 2)
    for value in (b'entra_probe', b'probe_db'):
        identity += struct.pack('!i', len(value)) + value
    wire = Wire(authentication(10, b'OAUTHBEARER\0\0') + authentication(0)
                + frame(b'Z', b'I') + frame(b'D', identity) + frame(b'Z', b'I'))
    monkeypatch.setattr(probe.socket, 'create_connection', lambda *a, **k: wire)
    probe.database_login(1234, 'private-token')
    token_frames = [part for part in wire.sent if b'private-token' in part]
    assert len(token_frames) == 1
    assert b'OAUTHBEARER\0' in token_frames[0]
    assert b'n,,\x01auth=Bearer private-token\x01\x01' in token_frames[0]


@pytest.mark.parametrize('code', ['28000', '28P01'])
def test_authentication_rejection_is_distinct(probe, monkeypatch, code):
    wire = Wire(authentication(10, b'OAUTHBEARER\0\0')
                + authentication(11, b'{"status":"invalid_token"}')
                + frame(b'E', b'C' + code.encode() + b'\0Mredacted\0\0'))
    monkeypatch.setattr(probe.socket, 'create_connection', lambda *a, **k: wire)
    with pytest.raises(probe.AuthenticationRejected, match=code):
        probe.database_login(1234, 'private-token')
    assert wire.sent[-1] == frame(b'p', b'\x01')


def test_infrastructure_failure_is_not_passing_negative_test(probe, monkeypatch):
    wire = Wire(authentication(10, b'OAUTHBEARER\0\0')
                + frame(b'E', b'CXX000\0Mserver failure\0\0'))
    monkeypatch.setattr(probe.socket, 'create_connection', lambda *a, **k: wire)
    with pytest.raises(RuntimeError, match='XX000'):
        probe.database_login(1234, 'private-token')


def test_invalid_signature_does_not_modify_claims(probe):
    token = 'header.payload.ABCD'
    changed = probe.bad_signature(token)
    assert changed.split('.')[:2] == token.split('.')[:2]
    assert changed != token


@pytest.mark.parametrize('claim,value', [('aud', 'wrong'), ('oid', 'wrong'),
                                       ('exp', 0), ('scp', 'other')])
def test_wrong_or_expired_token_not_used_by_probe(probe, claim, value):
    config = {'tenant': 'tenant', 'user_oid': 'operator', 'audience': 'audience', 'issuer': 'issuer'}
    payload = {'tid': 'tenant', 'oid': 'operator', 'aud': 'audience',
               'iss': 'issuer', 'exp': time.time() + 3600, 'scp': 'db.connect'}
    payload[claim] = value
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b'=').decode()
    with pytest.raises(ValueError, match='does not match'):
        probe.validate_claims('header.' + encoded + '.signature', config)


def test_container_guard_prevents_touching_unrelated_container(probe, monkeypatch):
    class Result:
        stdout = '[{"Config":{"Labels":{}}}]'
    monkeypatch.setattr(probe, 'command', lambda *a, **k: Result())
    with pytest.raises(ValueError, match='without the test label'):
        probe.guarded_container()
