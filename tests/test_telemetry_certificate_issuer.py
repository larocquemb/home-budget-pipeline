import stat
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/issue_telemetry_certificates.sh"


def test_telemetry_certificate_issuer_is_executable_and_valid_bash():
    assert SCRIPT.stat().st_mode & stat.S_IXUSR
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)

    result = subprocess.run(
        [str(SCRIPT), "--help"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "Intermediate CA" in result.stderr
    assert "--pki-dir" in result.stderr


def test_telemetry_certificate_issuer_preserves_the_pki_boundary():
    script = SCRIPT.read_text()

    assert "fluent-bit-loki-client.cnf" in script
    assert "monitoring_leaf.cnf" in script
    assert "refusing to replace existing identity" in script
    assert 'mktemp -d "$telemetry_leafs_dir/.telemetry-issuance.XXXXXX"' in script
    assert "YubiKey slot 9C certificate does not match" in script
    assert "--ask-pass" in script
    assert "--hash=SHA384" in script
    assert '--load-ca-privkey="$telemetry_intermediate_key_uri"' in script
    assert "--provider=\"$telemetry_pkcs11_provider\"" in script
    assert "-purpose \"$telemetry_purpose\"" in script
    assert "-checkhost \"$telemetry_hostname\"" in script
    assert "certificate and private key do not match" in script
    assert "make monitoring-gitops-check" in script
