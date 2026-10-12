"""Exercise NSS validation through Ansible, including Jinja interpretation."""
import base64
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("hosts,valid", [
    ("hosts: files mdns4_minimal [NOTFOUND=return] dns\n", True),
    ("hosts:\tfiles\tmdns4_minimal [NOTFOUND=return]\tdns systemd\n", True),
    ("hosts: files dns mdns4_minimal\n", False),
    ("hosts: files dns\n", False),
])
def test_mdns_order_validation_in_ansible(tmp_path, hosts, valid):
    executable = shutil.which("ansible-playbook")
    if not executable:
        pytest.skip("ansible-playbook is not installed")
    tasks = yaml.safe_load(
        (ROOT / "ops/monitoring/roles/monitoring/tasks/mdns.yml").read_text()
    )
    assertion = next(task for task in tasks if "ansible.builtin.assert" in task)
    playbook = tmp_path / "validate.yml"
    playbook.write_text(yaml.safe_dump([{
        "hosts": "localhost",
        "gather_facts": False,
        "vars": {"monitoring_mdns_nsswitch": {
            "content": base64.b64encode(("passwd: files\n" + hosts).encode()).decode(),
        }},
        "tasks": [assertion],
    }]))
    result = subprocess.run(
        [executable, "-i", "localhost,", "-c", "local", str(playbook)],
        env={**os.environ, "ANSIBLE_LOCAL_TEMP": str(tmp_path / "ansible")},
        capture_output=True, text=True, timeout=30,
    )
    if valid:
        assert result.returncode == 0, result.stdout + result.stderr
    else:
        assert result.returncode != 0
        assert "hosts entry in /etc/nsswitch.conf" in result.stdout + result.stderr
