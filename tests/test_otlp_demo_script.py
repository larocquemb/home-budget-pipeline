import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/demo_otlp.sh"


def test_otlp_demo_is_hardened_mtls_and_self_cleaning():
    text = SCRIPT.read_text(encoding="utf-8")

    assert os.access(SCRIPT, os.X_OK)
    assert "receipt-telemetry-client-tls" in text
    assert "telemetrygen@sha256:" in text
    assert '"--mtls"' in text
    assert '"--ca-cert", "/tls/ca.crt"' in text
    assert '"--client-cert", "/tls/tls.crt"' in text
    assert '"--client-key", "/tls/tls.key"' in text
    assert "automountServiceAccountToken: false" in text
    assert "runAsNonRoot: true" in text
    assert "fsGroup: 10001" in text
    assert "readOnlyRootFilesystem: true" in text
    assert "--rm" in text
    assert "delete pod" in text


def test_otlp_demo_is_exposed_by_make_and_documented():
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    runbook = (ROOT / "docs/opentelemetry.md").read_text(encoding="utf-8")

    assert "otlp-demo:" in makefile
    assert "./scripts/demo_otlp.sh" in makefile
    assert "make otlp-demo" in runbook
    assert 'OTLP_DEMO_SERVICE=home-budget-otlp-demo make otlp-demo' in runbook
    assert '{ resource.service.name = "home-budget-otlp-demo" }' in runbook
