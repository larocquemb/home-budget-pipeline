from pathlib import Path

import yaml


POSTGRES_MANIFEST = Path("k8s/postgres.yaml")
POSTGRES_IMAGE = (
    "ghcr.io/larocquemb/home-budget-postgres:"
    "18.6-oidc-7b6565afbc56bc8abcb75939d28c898377a10f00"
)


def _resources() -> dict[tuple[str, str], dict]:
    return {
        (resource["kind"], resource["metadata"]["name"]): resource
        for resource in yaml.safe_load_all(POSTGRES_MANIFEST.read_text())
    }


def test_kubernetes_runs_the_postgres_18_oauth_image():
    stateful_set = _resources()[("StatefulSet", "postgres")]
    container = stateful_set["spec"]["template"]["spec"]["containers"][0]

    assert container["image"] == POSTGRES_IMAGE
    assert container["args"] == [
        "-c",
        "oauth_validator_libraries=pg_oidc_validator",
        "-c",
        "pg_oidc_validator.authn_field=oid",
        "-c",
        "pg_oidc_validator.audience=$(POSTGRES_OAUTH_AUDIENCE)",
    ]
    assert container["env"] == [
        {
            "name": "POSTGRES_OAUTH_AUDIENCE",
            "valueFrom": {
                "secretKeyRef": {
                    "name": "ledger-oauth2-proxy-secret",
                    "key": "OAUTH2_PROXY_CLIENT_ID",
                }
            },
        }
    ]


def test_postgres_18_uses_a_fresh_volume_and_preserves_the_pg17_claim():
    resources = _resources()
    stateful_set = resources[("StatefulSet", "postgres")]
    pod_spec = stateful_set["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]

    assert resources[("PersistentVolumeClaim", "postgres18-data")]["spec"] == {
        "accessModes": ["ReadWriteOnce"],
        "storageClassName": "local-path",
        "resources": {"requests": {"storage": "10Gi"}},
    }
    assert {mount["name"]: mount["mountPath"] for mount in container["volumeMounts"]}[
        "postgres18-data"
    ] == "/var/lib/postgresql"
    assert "PGDATA" not in {item["name"] for item in container.get("env", [])}
    assert {
        volume["name"]: volume.get("persistentVolumeClaim", {}).get("claimName")
        for volume in pod_spec["volumes"]
    }["postgres18-data"] == "postgres18-data"
    assert stateful_set["spec"]["volumeClaimTemplates"][0]["metadata"]["name"] == "postgres-data"
    assert "postgres-data" not in {
        mount["name"] for mount in container["volumeMounts"]
    }
