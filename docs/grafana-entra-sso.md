# Grafana Microsoft Entra SSO

The operational Grafana instance at
`https://grafana.idc.brownrook.net` uses native Microsoft Entra ID sign-in.
The passwordless public telemetry demo at
`https://telemetry.idc.brownrook.com` remains a separate, restricted public
dashboard route and does not grant access to Grafana.

## Entra objects

| Object | Value |
| --- | --- |
| Tenant ID | `8b07f4bd-41e4-4106-8d49-00c5d79d35a2` |
| Application name | `Brown Rook Grafana` |
| Application/client ID | `670b17ca-a1e7-4c63-80e8-9d6a21c31fe4` |
| Application object ID | `07731147-e4de-4291-8e81-9f07a07fe1d4` |
| Enterprise application object ID | `ad75232a-e479-4d67-8165-79184474fdef` |
| Administrator group | `Grafana Admins` (`c526c241-3322-424a-9fdc-68288d0e5998`) |
| GrafanaAdmin app-role ID | `c4e670d9-29ca-4636-bd80-754d27dcc87d` |
| Redirect URI | `https://grafana.idc.brownrook.net/login/azuread` |

The enterprise application requires assignment. Assign users or groups to one
of its `Viewer`, `Editor`, `Admin`, or `GrafanaAdmin` app roles. Grafana rejects
sign-in when Entra does not return a valid Grafana role. Membership in
`Grafana Admins` currently grants the `GrafanaAdmin` app role.

## Secret boundary

The client credential named `grafana-sso-2026-09-15` expires on
2027-09-15. Its value is not committed to Git.

The controller reads the secret from:

```text
/Users/paul/brownrook-ca/secrets/grafana-entra-client-secret
```

`.env.monitoring` exposes only that path through
`MONITORING_GRAFANA_ENTRA_CLIENT_SECRET_FILE`. The Ansible role validates the
source file as mode `0400` or `0600`, then installs a root-owned environment
file at `/etc/grafana/grafana-entra.env` with mode `0640`. Secret-bearing tasks
use `no_log`.

## Reconcile and verify

From the repository root, run:

```sh
make monitoring-gitops-syntax
make monitoring-gitops-check
make monitoring-gitops-apply
```

Open `https://grafana.idc.brownrook.net/login` and select **Microsoft Entra
ID**. Sign in as an assigned user, then confirm that the expected Grafana role
was applied. The playbook independently checks that `/login/azuread` redirects
to this tenant before it reconciles dashboards.

Do not disable Grafana basic authentication or its local login form. The local
`admin` identity is the break-glass path if Entra, DNS, or the application
credential is unavailable. Keep its password outside Git and test it after an
SSO change.

## Rotate the client credential

1. Create a new credential on the `Brown Rook Grafana` Entra application.
2. Replace the external secret file atomically and preserve mode `0600`.
3. Run the monitoring GitOps check and apply commands.
4. Verify an Entra login in a fresh private browser session.
5. Delete the previous Entra credential only after the new login succeeds.

If SSO fails, use the local administrator login, inspect
`journalctl -u grafana-server`, restore a valid external client secret, and
reapply the role. Never place a credential value in a command, commit, issue,
or runbook.
