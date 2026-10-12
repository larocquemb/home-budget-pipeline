#!/usr/bin/python
"""Consolidate the explicitly audited dashboards without deleting unique queries."""
import base64
from collections import Counter
import copy
import json
import os
from pathlib import Path
import re
import ssl
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from ansible.module_utils.basic import AnsibleModule


PROM = 'home-budget-prometheus'
MISSING_PROM = 'dfvym4tkfc16oa'
OVERVIEW = 'brownrook-k3s-node'
FULL = 'rYdddlPWk'
RETIRED_NODE = 'k3s-node'
RETIRED_LOGS = 'pacq65g'


def panels(items):
    for panel in items:
        yield panel
        yield from panels(panel.get('panels', []))


def query_signature(dashboard):
    result = Counter()
    for panel in panels(dashboard.get('panels', [])):
        targets = panel.get('targets', [])
        if targets:
            expressions = tuple(target.get('expr', target.get('rawSql', target.get('query', '')))
                                for target in targets)
            result[(panel.get('title'), expressions)] += 1
        elif panel.get('type') != 'row':
            result[(panel.get('title'), json.dumps(panel.get('options', {}), sort_keys=True))] += 1
    return result


def rebind(value, old, new):
    if isinstance(value, dict):
        return {key: rebind(item, old, new) for key, item in value.items()}
    if isinstance(value, list):
        return [rebind(item, old, new) for item in value]
    return new if value == old else value


def atomic_json(path, data):
    mode = path.stat().st_mode & 0o777
    owner = path.stat()
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as handle:
        json.dump(data, handle, indent=2)
        handle.write('\n')
        temporary = Path(handle.name)
    temporary.chmod(mode)
    os.chown(temporary, owner.st_uid, owner.st_gid)
    temporary.replace(path)


def overview_file():
    roots = [Path('/var/lib/grafana/dashboards'), Path('/etc/grafana/dashboards')]
    for config in Path('/etc/grafana/provisioning/dashboards').glob('*'):
        if config.suffix in ('.yml', '.yaml'):
            for path in re.findall(r'^\s*path:\s*[\'"]?([^\n\'"]+)', config.read_text(), re.M):
                roots.append(Path(path.strip()))
    found = set()
    for root in roots:
        if root.is_dir():
            for path in root.rglob('*.json'):
                try:
                    document = json.loads(path.read_text())
                except (ValueError, OSError):
                    continue
                if document.get('uid') == OVERVIEW:
                    found.add(path.resolve())
    if len(found) != 1:
        raise RuntimeError('Expected exactly one provisioned source for the K3s overview; found ' + str(len(found)))
    return found.pop()


def provider_folder_update(source, folder_uid):
    """Keep the provisioner from recreating the old folder after API moves."""
    matches = []
    for config in Path('/etc/grafana/provisioning/dashboards').glob('*'):
        if config.suffix not in ('.yml', '.yaml'):
            continue
        contents = config.read_text()
        roots = re.findall(r'^\s*path:\s*[\'"]?([^\n\'"]+)', contents, re.M)
        if not any(source.is_relative_to(Path(root.strip()).resolve()) for root in roots):
            continue
        folders = list(re.finditer(r'^([ \t]*)folder:[ \t]*[\'"]?(Brown Rook|Infrastructure)[\'"]?[ \t]*$', contents, re.M))
        if len(folders) != 1:
            raise RuntimeError('Expected one infrastructure folder in ' + str(config))
        match = folders[0]
        replacement = match.group(1) + 'folder: Infrastructure'
        uid_match = re.search(r'^\s*folderUid:\s*[\'"]?([^\n\'"]+)', contents, re.M)
        if uid_match and uid_match.group(1).strip() != folder_uid:
            raise RuntimeError('Provisioned infrastructure folder UID does not match Grafana')
        if not uid_match:
            replacement += '\n' + match.group(1) + 'folderUid: ' + folder_uid
        updated = contents[:match.start()] + replacement + contents[match.end():]
        matches.append((config, contents, updated))
    if len(matches) != 1:
        raise RuntimeError('Expected one infrastructure dashboard provider')
    return matches[0]


def main():
    module = AnsibleModule(argument_spec={
        'url': {'required': True},
        'username': {'required': True},
        'password': {'required': True, 'no_log': True},
        'ca_path': {'required': True},
        'namespace': {'default': 'default'},
        'telemetry': {'type': 'dict', 'required': True},
        'dashboards': {'type': 'list', 'elements': 'dict', 'default': []},
        'demos_uid': {'default': 'demos'},
        'prometheus_provisioning': {'required': True},
    }, supports_check_mode=True)
    settings = module.params
    context = ssl.create_default_context(cafile=settings['ca_path'])
    authorization = 'Basic ' + base64.b64encode(
        (settings['username'] + ':' + settings['password']).encode()).decode()

    def api(path, method='GET', body=None):
        request = Request(settings['url'].rstrip('/') + path,
                          data=json.dumps(body).encode() if body is not None else None,
                          headers={'Authorization': authorization, 'Content-Type': 'application/json'},
                          method=method)
        try:
            with urlopen(request, context=context, timeout=30) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except HTTPError as error:
            if error.code == 404 and method == 'GET':
                return None
            raise RuntimeError('Grafana API ' + method + ' ' + path + ' returned HTTP ' + str(error.code)) from None

    prefix = '/apis/dashboard.grafana.app/v1/namespaces/' + quote(settings['namespace']) + '/dashboards/'
    documents = {uid: api('/api/dashboards/uid/' + uid)
                 for uid in (OVERVIEW, FULL, RETIRED_NODE, RETIRED_LOGS)}
    desired = settings['telemetry']
    telemetry_path = '/apis/dashboard.grafana.app/v2/namespaces/' + quote(settings['namespace']) + '/dashboards/' + desired['metadata']['name']
    changes = []
    try:
        sources = api('/api/datasources')
        if not any(source['uid'] == PROM for source in sources):
            raise RuntimeError('Retained Prometheus datasource is missing')
        if any(source['uid'] == MISSING_PROM for source in sources):
            raise RuntimeError('The retired Prometheus source has returned; audit alerts and library panels before deleting it')
        if not documents[OVERVIEW] or not documents[FULL]:
            raise RuntimeError('The retained node overview or detailed dashboard is missing')
        retired = documents[RETIRED_NODE]
        if retired and (not query_signature(retired['dashboard']) or
                        query_signature(retired['dashboard']) != query_signature(documents[FULL]['dashboard'])):
            raise RuntimeError('The retired node dashboard has unique content; refusing deletion')
        retired_logs = documents[RETIRED_LOGS]
        if retired_logs:
            log_panels = list(panels(retired_logs['dashboard'].get('panels', [])))
            expected = '{cluster="brownrook-k3s1", namespace="home-budget", app="receipt-processor", container="receipt-processor"}'
            if len(log_panels) != 1 or len(log_panels[0].get('targets', [])) != 1 or \
                    re.sub(r'\s+', ' ', log_panels[0]['targets'][0].get('expr', '')).strip() != expected:
                raise RuntimeError('The standalone receipt log dashboard changed; refusing deletion')
        # Establish every migration precondition before writing anything.
        source_file = overview_file()
        infrastructure_uid = documents[OVERVIEW]['meta']['folderUid']
        provider_file, provider_original, provider_updated = provider_folder_update(source_file, infrastructure_uid)
        original_overview = json.loads(source_file.read_text())
        repaired_overview = rebind(original_overview, MISSING_PROM, PROM)
        prometheus_file = Path(settings['prometheus_provisioning'])
        original_prometheus = prometheus_file.read_text()
        if 'uid: ' + PROM not in original_prometheus:
            raise RuntimeError('Unexpected Prometheus provisioning definition')
        repaired_prometheus = original_prometheus.replace('isDefault: false', 'isDefault: true')
        full_resource = api(prefix + FULL)
        if full_resource is None:
            raise RuntimeError('The retained detailed dashboard cannot be read through the native API')
        updated_full = copy.deepcopy(full_resource)
        updated_full['metadata'].setdefault('annotations', {})['grafana.app/folder'] = documents[OVERVIEW]['meta']['folderUid']
        for variable in updated_full['spec'].get('templating', {}).get('list', []):
            if variable.get('name') == 'ds_prometheus':
                variable['current'] = {'text': PROM, 'value': PROM}
                variable['regex'] = '/^home-budget-prometheus$/'
        current_telemetry = api(telemetry_path)
        if current_telemetry is None:
            raise RuntimeError('Retained receipt telemetry dashboard is missing')
        log_element = desired['spec']['elements'].get('panel-13', {})
        if log_element.get('spec', {}).get('vizConfig', {}).get('group') != 'logs':
            raise RuntimeError('The receipt log migration panel is missing')
        folders_prefix = '/apis/folder.grafana.app/v1/namespaces/' + quote(settings['namespace']) + '/folders'
        infrastructure_folder = api(folders_prefix + '/' + infrastructure_uid)
        if infrastructure_folder is None:
            raise RuntimeError('Existing infrastructure folder is missing')
        demos_folder = api(folders_prefix + '/' + settings['demos_uid'])
        additional = []
        for dashboard in settings['dashboards']:
            path = '/apis/dashboard.grafana.app/v2/namespaces/' + quote(settings['namespace']) + '/dashboards/' + dashboard['metadata']['name']
            current = api(path)
            if current is None:
                raise RuntimeError('Expected retained dashboard is missing: ' + dashboard['metadata']['name'])
            additional.append((path, dashboard, current))

        def change(label, action):
            changes.append(label)
            if not module.check_mode:
                action()

        if infrastructure_folder['spec']['title'] != 'Infrastructure':
            updated_folder = copy.deepcopy(infrastructure_folder)
            updated_folder['spec']['title'] = 'Infrastructure'
            change('Rename Brown Rook folder to Infrastructure',
                   lambda: api(folders_prefix + '/' + infrastructure_uid, 'PUT', updated_folder))
        if demos_folder is None:
            demos = {'apiVersion': 'folder.grafana.app/v1', 'kind': 'Folder',
                     'metadata': {'name': settings['demos_uid']}, 'spec': {'title': 'Demos'}}
            change('Create Demos folder', lambda: api(folders_prefix, 'POST', demos))
        if provider_updated != provider_original:
            def update_provider():
                provider_file.write_text(provider_updated)
                api('/api/admin/provisioning/dashboards/reload', 'POST')
            change('Keep infrastructure provisioner in the Infrastructure folder', update_provider)
        for path, dashboard, current in additional:
            folder = dashboard['metadata']['annotations']['grafana.app/folder']
            if current['spec'] != dashboard['spec'] or current['metadata'].get('annotations', {}).get('grafana.app/folder') != folder:
                updated = copy.deepcopy(current)
                updated['spec'] = dashboard['spec']
                updated['metadata'].setdefault('annotations', {})['grafana.app/folder'] = folder
                change('Organize and add execution-host selection to ' + dashboard['spec']['title'],
                       lambda path=path, updated=updated: api(path, 'PUT', updated))

        if repaired_overview != original_overview:
            change('Repair provisioned K3s overview datasource', lambda: atomic_json(source_file, repaired_overview))
            if not module.check_mode:
                api('/api/admin/provisioning/dashboards/reload', 'POST')
                for _ in range(10):
                    repaired = api('/api/dashboards/uid/' + OVERVIEW)
                    if MISSING_PROM not in json.dumps(repaired):
                        break
                    time.sleep(1)
                if MISSING_PROM in json.dumps(repaired):
                    raise RuntimeError('The provisioned overview still references the missing datasource')
        if repaired_prometheus != original_prometheus or not next(source['isDefault'] for source in sources if source['uid'] == PROM):
            def write_prometheus():
                prometheus_file.write_text(repaired_prometheus)
                api('/api/admin/provisioning/datasources/reload', 'POST')
                if not api('/api/datasources/uid/' + PROM).get('isDefault'):
                    raise RuntimeError('Retained Prometheus datasource did not become default')
            change('Make retained Prometheus datasource default', write_prometheus)
        if updated_full != full_resource:
            change('Move Node Exporter Full to Brown Rook and select retained datasource',
                   lambda: api(prefix + FULL, 'PUT', updated_full))
        if current_telemetry['spec'] != desired['spec']:
            telemetry = copy.deepcopy(current_telemetry)
            telemetry['spec'] = desired['spec']
            change('Add receipt processor logs and worker overlap to receipt telemetry',
                   lambda: api(telemetry_path, 'PUT', telemetry))
        # Verify migration destinations before removing the audited copies.
        if not module.check_mode:
            saved = api(telemetry_path)
            if saved['spec']['elements'].get('panel-13') != log_element:
                raise RuntimeError('Receipt log panel migration did not persist')
            if query_signature(api('/api/dashboards/uid/' + FULL)['dashboard']) != query_signature(documents[FULL]['dashboard']):
                raise RuntimeError('Detailed node queries changed during migration')
        for uid, document in ((RETIRED_NODE, retired), (RETIRED_LOGS, retired_logs)):
            if document:
                if not module.check_mode and api('/api/dashboards/uid/' + uid)['dashboard'] != document['dashboard']:
                    raise RuntimeError('Dashboard changed during cleanup; refusing deletion of ' + uid)
                change('Remove consolidated dashboard ' + uid, lambda uid=uid: api(prefix + uid, 'DELETE'))
        module.exit_json(changed=bool(changes), actions=changes)
    except Exception as error:
        module.fail_json(msg=str(error), changed=bool(changes), actions=changes)


if __name__ == '__main__':
    main()
