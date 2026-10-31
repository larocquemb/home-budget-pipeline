#!/usr/bin/env python3
"""Isolated KAN-137 Entra/PostgreSQL authentication probe (Python standard library)."""
import argparse
import base64
import json
import os
from pathlib import Path
import shlex
import socket
import struct
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

CONTAINER = 'brownrook-pg-entra-validation'
IMAGE = 'localhost/brownrook-postgres-rhel-oidc:18.6-test'
LABEL = 'io.brownrook.entra-validation'
REMOTE = '.local/state/brownrook/kan137/postgres-rhel'
TENANT = '8b07f4bd-41e4-4106-8d49-00c5d79d35a2'


def configuration(path):
    config = json.loads(path.read_text())
    for name in ('tenant', 'user_oid', 'audience', 'client_id'):
        if str(uuid.UUID(config[name])) != config[name]:
            raise ValueError('Invalid configuration identifier')
    if (config['tenant'] != TENANT
            or config['issuer'] != f'https://login.microsoftonline.com/{TENANT}/v2.0'
            or config['scope'] != f'api://{config["audience"]}/db.connect'):
        raise ValueError('Unexpected validation tenant, issuer or scope')
    return config


def command(args, payload=None, allow_error=False):
    result = subprocess.run(args, input=payload, text=True, capture_output=True, timeout=120)
    if result.returncode and not allow_error:
        # Do not dump process output or bearer-token-related server errors.
        raise RuntimeError(f'{args[0]} operation failed (exit {result.returncode}); output withheld.')
    return result


def guarded_container():
    result = command(['podman', 'inspect', CONTAINER])
    value = json.loads(result.stdout)[0]
    if value.get('Config', {}).get('Labels', {}).get(LABEL) != 'kan137':
        raise ValueError('Refusing to operate on a container without the test label')


def admin(sql):
    return command(['podman', 'exec', '-i', CONTAINER, 'psql', '-X', '-U', 'postgres',
                    '-d', 'postgres', '-At', '-v', 'ON_ERROR_STOP=1'], sql).stdout.strip()


def write_container_file(name, data):
    if name not in ('pg_hba.conf', 'pg_ident.conf'):
        raise ValueError('Unsupported test configuration file')
    setting = 'hba_file' if name == 'pg_hba.conf' else 'ident_file'
    path = admin('SHOW ' + setting + ';')
    if not path.startswith('/var/lib/pgsql/data/') or Path(path).name != name:
        raise ValueError('Unexpected configuration path in the temporary container')
    command(['podman', 'exec', '-i', CONTAINER, '/bin/sh', '-c',
             'cat > "$1"', 'validation-write', path], data)


def policy(config, name):
    guarded_container()
    audience = config['audience']
    scope = config['scope']
    if name == 'wrong-audience':
        audience = 'deliberately-wrong-test-audience'
    elif name == 'missing-audience':
        audience = ''
    elif name == 'wrong-scope':
        scope = f'api://{config["audience"]}/not-granted'
    elif name != 'baseline':
        raise ValueError('Unknown test policy')
    write_container_file('pg_hba.conf',
        'local all all trust\n'
        f'host probe_db all 0.0.0.0/0 oauth issuer="{config["issuer"]}" '
        f'scope="{scope}" map=probe_map\n'
        'host all all 0.0.0.0/0 reject\nhost all all ::/0 reject\n')
    admin(f"ALTER SYSTEM SET pg_oidc_validator.audience = '{audience}';")
    admin('SELECT pg_reload_conf();')
    if admin('SELECT count(*) FROM pg_hba_file_rules WHERE error IS NOT NULL;') != '0':
        raise RuntimeError('Invalid test authentication rule')
    for _ in range(30):
        if admin('SHOW pg_oidc_validator.audience;') == audience:
            print('Test policy: ' + name)
            return
        time.sleep(0.1)
    raise RuntimeError('Test policy did not reload')


def start(config):
    if command(['podman', 'container', 'exists', CONTAINER], allow_error=True).returncode == 0:
        raise ValueError('Test container already exists. Inspect it or run the stop command first.')
    created = False
    try:
        command(['podman', 'run', '--detach', '--name', CONTAINER,
                 '--label', LABEL + '=kan137', '--publish', '127.0.0.1:15433:5432',
                 '--memory=1g', '--memory-swap=1g', '--cpus=2', '--pids-limit=256',
                 '--env', 'POSTGRESQL_USER=probe_user',
                 '--env', 'POSTGRESQL_PASSWORD=isolated-test-only',
                 '--env', 'POSTGRESQL_DATABASE=probe_db', IMAGE, 'run-postgresql',
                 '-c', 'shared_preload_libraries=pg_oidc_validator',
                 '-c', 'oauth_validator_libraries=pg_oidc_validator',
                 '-c', 'pg_oidc_validator.authn_field=oid'])
        created = True
        for _ in range(90):
            result = command(['podman', 'exec', CONTAINER, 'psql', '-X', '-U', 'postgres',
                              '-d', 'postgres', '-Atc',
                              "SELECT current_setting('oauth_validator_libraries');"],
                             allow_error=True)
            if result.returncode == 0 and result.stdout.strip() == 'pg_oidc_validator':
                break
            time.sleep(1)
        else:
            raise RuntimeError('Temporary PostgreSQL did not become ready')
        admin('CREATE ROLE entra_probe LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;\n'
              'CREATE ROLE denied_probe LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;')
        write_container_file('pg_ident.conf', f'probe_map {config["user_oid"]} entra_probe\n')
        policy(config, 'baseline')
    except Exception:
        if created:
            command(['podman', 'rm', '--force', CONTAINER], allow_error=True)
        raise
    print('Ready: temporary database on Arsene 127.0.0.1:15433; no host volumes.')
    print('Container can reach Microsoft discovery/JWKS over its normal outbound network.')


def post_form(url, fields):
    request = urllib.request.Request(url, urllib.parse.urlencode(fields).encode(),
                                     headers={'Content-Type': 'application/x-www-form-urlencoded'})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        return json.loads(error.read())


def claims(token):
    # Sanity checks only. PostgreSQL must still validate the actual signature.
    raw = token.split('.')[1]
    return json.loads(base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)))


def validate_claims(token, config):
    value = claims(token)
    if (value.get('tid') != config['tenant'] or value.get('oid') != config['user_oid']
            or value.get('aud') != config['audience'] or value.get('iss') != config['issuer']
            or 'db.connect' not in value.get('scp', '').split()
            or value.get('exp', 0) < time.time() + 120):
        raise ValueError('Token identity, scope, audience or validity does not match this test')


def login(config, token_path):
    base = f'https://login.microsoftonline.com/{config["tenant"]}/oauth2/v2.0/'
    device = post_form(base + 'devicecode', {'client_id': config['client_id'], 'scope': config['scope']})
    if 'device_code' not in device:
        raise RuntimeError('Device authorization failed: ' + device.get('error', 'unknown'))
    print(device['message'], flush=True)
    deadline = time.monotonic() + device['expires_in']
    interval = max(device.get('interval', 5), 1)
    while time.monotonic() < deadline:
        time.sleep(interval)
        result = post_form(base + 'token', {
            'grant_type': 'urn:ietf:params:oauth:grant-type:device_code',
            'client_id': config['client_id'], 'device_code': device['device_code']})
        if 'access_token' in result:
            token = result['access_token']
            validate_claims(token, config)
            token_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            token_path.parent.chmod(0o700)
            fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as out:
                os.fchmod(out.fileno(), 0o600)
                out.write(token)
            print('Entra token obtained; stored privately, never printed.')
            return
        error = result.get('error', 'unknown')
        if error == 'slow_down':
            interval += 5
        elif error != 'authorization_pending':
            raise RuntimeError('Entra token request failed: ' + error
                               + '. Tenant policy may require administrator consent.')
    raise RuntimeError('Device authorization expired')


class AuthenticationRejected(Exception):
    pass


def receive_exact(sock, size):
    data = bytearray()
    while len(data) < size:
        block = sock.recv(size - len(data))
        if not block:
            raise RuntimeError('Database closed the connection')
        data.extend(block)
    return bytes(data)


def receive(sock):
    kind = receive_exact(sock, 1)
    size = struct.unpack('!I', receive_exact(sock, 4))[0]
    if not 4 <= size <= 1024 * 1024:
        raise RuntimeError('Unexpected PostgreSQL message length')
    return kind, receive_exact(sock, size - 4)


def send(sock, kind, data):
    sock.sendall(kind + struct.pack('!I', len(data) + 4) + data)


def database_login(port, token, role='entra_probe'):
    # Only through loopback/SSH. This tiny client is test code, not an app driver.
    with socket.create_connection(('127.0.0.1', port), timeout=60) as sock:
        body = struct.pack('!I', 196608) + b'user\0' + role.encode() + b'\0database\0probe_db\0\0'
        sock.sendall(struct.pack('!I', len(body) + 4) + body)
        sent_token = authenticated = False
        while True:
            kind, data = receive(sock)
            if kind == b'E':
                fields = {part[:1]: part[1:] for part in data.split(b'\0') if part}
                code = fields.get(b'C', b'').decode('ascii', errors='replace')
                if code in ('28000', '28P01') and sent_token:
                    raise AuthenticationRejected('OAuth rejected (SQLSTATE ' + code + ')')
                raise RuntimeError('Database error (SQLSTATE ' + code + ')')
            if kind == b'R':
                code = struct.unpack('!I', data[:4])[0]
                if code == 10 and not sent_token:
                    if b'OAUTHBEARER' not in data[4:].split(b'\0'):
                        raise RuntimeError('Server did not require OAUTHBEARER')
                    response = b'n,,\x01auth=Bearer ' + token.encode('ascii') + b'\x01\x01'
                    send(sock, b'p', b'OAUTHBEARER\0' + struct.pack('!I', len(response)) + response)
                    sent_token = True
                elif code == 11 and sent_token:
                    send(sock, b'p', b'\x01')
                elif code == 0 and sent_token:
                    authenticated = True
                else:
                    raise RuntimeError('Unexpected authentication method or sequence')
            if kind == b'Z':
                if not authenticated:
                    raise RuntimeError('No verified OAuth authentication exchange')
                break
        send(sock, b'Q', b'SELECT current_user, current_database();\0')
        rows = []
        while True:
            kind, data = receive(sock)
            if kind == b'E':
                raise RuntimeError('Authenticated query failed')
            if kind == b'D':
                count = struct.unpack('!H', data[:2])[0]
                row, offset = [], 2
                for _ in range(count):
                    size = struct.unpack('!i', data[offset:offset + 4])[0]
                    offset += 4
                    if size < 0:
                        raise RuntimeError('Unexpected NULL in identity query')
                    row.append(data[offset:offset + size].decode())
                    offset += size
                rows.append(row)
            if kind == b'Z':
                break
        send(sock, b'X', b'')
        if rows != [[role, 'probe_db']]:
            raise RuntimeError('Authenticated database identity did not match')


def bad_signature(token):
    header, payload, signature = token.split('.')
    signature = ('A' if signature[0] != 'A' else 'B') + signature[1:]
    return '.'.join((header, payload, signature))


def remote_policy(name):
    args = ['python3', REMOTE + '/postgres_entra_probe.py', '--config', REMOTE + '/config.json',
            'policy', '--name', name]
    command(['ssh', '-o', 'BatchMode=yes', 'arsene', shlex.join(args)])


def check(config, token_path):
    if token_path.stat().st_mode & 0o077:
        raise ValueError('Token file must have mode 0600')
    token = token_path.read_text().strip()
    validate_claims(token, config)
    remote_policy('baseline')
    # Reserve a currently unused local port, then ask SSH to claim it explicitly.
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    tunnel = subprocess.Popen(['ssh', '-N', '-o', 'BatchMode=yes', '-o', 'ExitOnForwardFailure=yes',
                               '-L', f'127.0.0.1:{port}:127.0.0.1:15433', 'arsene'],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    passed = []
    try:
        for _ in range(100):
            if tunnel.poll() is not None:
                raise RuntimeError('SSH tunnel could not start')
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError('SSH tunnel did not become ready')

        def positive(name):
            database_login(port, token)
            passed.append(name)
            print('PASS: ' + name, flush=True)

        def negative(name, candidate, role='entra_probe'):
            try:
                database_login(port, candidate, role)
            except AuthenticationRejected:
                passed.append(name)
                print('PASS: ' + name, flush=True)
            else:
                raise RuntimeError('SECURITY TEST FAILED: ' + name + ' was accepted')

        positive('valid Entra token and mapped database role')
        negative('unmapped database role rejected', token, 'denied_probe')
        negative('invalid signature rejected', bad_signature(token))
        for name in ('wrong-audience', 'missing-audience', 'wrong-scope'):
            try:
                remote_policy(name)
                negative(name + ' rejected', token)
            finally:
                remote_policy('baseline')
            positive('valid login restored after ' + name)
        report = {'passed': passed, 'not_tested': ['validly signed expired token',
                  'validly signed token from another issuer', 'client applications such as pgAdmin',
                  'persistence, backup/restore, vendor support'], 'production_changed': False}
        output = token_path.parent / 'auth-report.json'
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as out:
            os.fchmod(out.fileno(), 0o600)
            json.dump(report, out, indent=2)
        print('Saved authentication report: ' + str(output))
    finally:
        tunnel.terminate()
        try:
            tunnel.wait(timeout=5)
        except subprocess.TimeoutExpired:
            tunnel.kill()
            tunnel.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    sub = parser.add_subparsers(dest='action', required=True)
    for action in ('start', 'stop'):
        sub.add_parser(action)
    policy_parser = sub.add_parser('policy')
    policy_parser.add_argument('--name', required=True,
                               choices=['baseline', 'wrong-audience', 'missing-audience', 'wrong-scope'])
    for action in ('login', 'check'):
        sub.add_parser(action)
    args = parser.parse_args()
    config = configuration(args.config)
    token_path = args.config.parent / 'access-token.private'
    if args.action == 'start':
        start(config)
    elif args.action == 'stop':
        guarded_container()
        command(['podman', 'rm', '--force', CONTAINER])
        print('Removed the labeled temporary database container.')
    elif args.action == 'policy':
        policy(config, args.name)
    elif args.action == 'login':
        login(config, token_path)
    else:
        check(config, token_path)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired, AuthenticationRejected) as error:
        raise SystemExit(str(error)) from None
