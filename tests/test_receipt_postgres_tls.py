import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/prepare_receipt_postgres_tls.sh"


@pytest.mark.parametrize("host,san,template_name", [
    ("m4pro.local", "DNS:m4pro.local", "dns_name"),
    ("192.168.2.159", "IP Address:192.168.2.159", "ip_address"),
])
def test_prepare_postgres_identity_and_preserve_existing_key(tmp_path, host, san, template_name):
    if not shutil.which("openssl"):
        pytest.skip("openssl is not installed")
    directory = tmp_path / "tls"
    subprocess.run(["bash", str(SCRIPT), host, str(directory)], check=True, capture_output=True)
    request = subprocess.check_output(
        ["openssl", "req", "-in", str(directory / "server.csr"), "-noout", "-text"],
        text=True,
    )
    assert san in request
    assert "TLS Web Server Authentication" in request
    assert f'{template_name} = "{host}"' in (directory / "server.tmpl").read_text()
    key = directory / "server.key"
    assert key.stat().st_mode & 0o777 == 0o600
    original = key.read_bytes()
    repeated = subprocess.run(["bash", str(SCRIPT), host, str(directory)], capture_output=True)
    assert repeated.returncode != 0
    assert key.read_bytes() == original


@pytest.mark.parametrize("host", ["256.1.2.3", "-m4pro.local", "m4pro.local\ndns_name=other.local"])
def test_invalid_identity_does_not_create_files(tmp_path, host):
    directory = tmp_path / "tls"
    result = subprocess.run(["bash", str(SCRIPT), host, str(directory)], capture_output=True)
    assert result.returncode != 0
    assert not directory.exists()
