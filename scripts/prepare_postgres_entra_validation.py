#!/usr/bin/env python3
"""Prepare two dedicated Entra test registrations; never modify the Ledger app."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import uuid

ROOT = Path(__file__).resolve().parents[1]
TENANT = '8b07f4bd-41e4-4106-8d49-00c5d79d35a2'
USER = '6b55f75b-86cc-4068-b44c-4434a0e90a10'
LEDGER = 'fc842d13-3eb9-479f-96b7-b13a8c9795d9'
GRAPH = 'https://graph.microsoft.com/v1.0'
NAMES = {'resource': 'Brown Rook PostgreSQL Validation API',
         'client': 'Brown Rook PostgreSQL Validation Client'}


def az(*args):
    result = subprocess.run(['az', *args, '--output', 'json', '--only-show-errors'],
                            capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError('Azure CLI operation failed. Check your login, tenant and '
                           'application-management permissions; no credentials were printed.')
    return json.loads(result.stdout) if result.stdout.strip() else None


def save(path, value):
    temporary = path.with_suffix('.json.new')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as out:
        os.fchmod(out.fileno(), 0o600)
        json.dump(value, out, indent=2)
        out.write('\n')
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print('Dedicated test registrations:')
    for name in NAMES.values():
        print(f'  {name}')
    print('Single-tenant; delegated db.connect permission; no client secrets; '
          'no tenant-wide consent grant; existing Ledger registration unchanged.')
    if not args.apply:
        print('Preview only. Use --apply to create/reconcile these test registrations.')
        return

    if az('account', 'show').get('tenantId') != TENANT:
        raise ValueError('Wrong Azure tenant. Select the Brown Rook tenant first.')
    if az('ad', 'signed-in-user', 'show').get('id') != USER:
        raise ValueError('Wrong signed-in user for this validation configuration.')
    directory = ROOT / '.validation/kan137/entra'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    state_path = directory / 'registrations.json'
    if state_path.exists():
        state = json.loads(state_path.read_text())
        if state.get('tenant') != TENANT or state.get('user_oid') != USER:
            raise ValueError('Saved validation identity does not match.')
    else:
        state = {'tenant': TENANT, 'user_oid': USER, 'marker': str(uuid.uuid4()),
                 'scope_id': str(uuid.uuid4())}
        save(state_path, state)

    def graph(method, path, body=None):
        command = ['rest', '--method', method, '--url', GRAPH + path]
        if body is not None:
            request = directory / 'request.json'
            save(request, body)
            command += ['--headers', 'Content-Type=application/json',
                        '--body', '@' + str(request)]
        return az(*command)

    # A unique marker recovers a successful POST even if its response was lost.
    for kind, name in NAMES.items():
        note = f'Brown Rook KAN-137 isolated PostgreSQL validation {state["marker"]} {kind}'
        if kind not in state:
            candidates = az('ad', 'app', 'list', '--display-name', name)
            matches = [app for app in candidates if app.get('notes') == note]
            if len(matches) > 1:
                raise ValueError('Multiple matching test applications; inspect before retrying.')
            app = matches[0] if matches else graph('POST', '/applications', {
                'displayName': name, 'signInAudience': 'AzureADMyOrg', 'notes': note})
            state[kind] = {'id': app['id'], 'appId': app['appId']}
            save(state_path, state)
        app = graph('GET', '/applications/' + state[kind]['id'])
        if (app.get('notes') != note or app.get('appId') != state[kind]['appId']
                or app.get('appId') == LEDGER):
            raise ValueError('Refusing to modify an application not owned by this test.')

    resource = state['resource']
    scope = {'id': state['scope_id'], 'value': 'db.connect', 'isEnabled': True,
             'type': 'User',
             'adminConsentDisplayName': 'Connect to isolated PostgreSQL validation',
             'adminConsentDescription': 'Connect as yourself to the isolated Brown Rook PostgreSQL test.',
             'userConsentDisplayName': 'Connect to isolated PostgreSQL validation',
             'userConsentDescription': 'Connect as yourself to the isolated Brown Rook PostgreSQL test.'}
    graph('PATCH', '/applications/' + resource['id'], {
        'identifierUris': ['api://' + resource['appId']],
        'api': {'requestedAccessTokenVersion': 2, 'oauth2PermissionScopes': [scope]}})
    graph('PATCH', '/applications/' + state['client']['id'], {
        'isFallbackPublicClient': True,
        'requiredResourceAccess': [{'resourceAppId': resource['appId'],
                                   'resourceAccess': [{'id': state['scope_id'], 'type': 'Scope'}]}]})

    for kind in NAMES:
        app_id = state[kind]['appId']
        principals = az('ad', 'sp', 'list', '--filter', f"appId eq '{app_id}'")
        if len(principals) > 1:
            raise ValueError('Multiple service principals; inspect before retrying.')
        principal = principals[0] if principals else graph('POST', '/servicePrincipals', {'appId': app_id})
        state[kind]['servicePrincipalId'] = principal['id']
        save(state_path, state)

    # Confirm server-side settings rather than treating successful PATCH as proof.
    actual_api = graph('GET', '/applications/' + resource['id'])
    actual_client = graph('GET', '/applications/' + state['client']['id'])
    if (actual_api['api'].get('requestedAccessTokenVersion') != 2
            or not any(s.get('id') == state['scope_id'] and s.get('isEnabled')
                       and s.get('value') == 'db.connect'
                       for s in actual_api['api'].get('oauth2PermissionScopes', []))
            or actual_client.get('isFallbackPublicClient') is not True):
        raise RuntimeError('Registration read-back did not match the requested configuration.')
    config = {'tenant': TENANT, 'user_oid': USER,
              'issuer': f'https://login.microsoftonline.com/{TENANT}/v2.0',
              'audience': resource['appId'],
              'scope': f'api://{resource["appId"]}/db.connect',
              'client_id': state['client']['appId']}
    save(directory / 'config.json', config)
    print(json.dumps(config, indent=2))
    print('Registration configuration verified. Consent and actual token/database login are not yet tested.')
    print('Saved registration object IDs for later cleanup in ' + str(state_path))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError) as error:
        raise SystemExit(str(error)) from None
