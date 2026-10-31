# PostgreSQL 18 OAuth image

This image extends the official `postgres:18.6-bookworm` image (pinned by its
multi-platform digest) with Percona's
experimental [`pg_oidc_validator`](https://github.com/Percona-Lab/pg_oidc_validator).
The source is pinned to commit
`ba9c4cb2dc9bd2e9779c7a33f7a30c9bb8c97862` and built from source.

The repository patch makes two security-relevant changes:

- `pg_oidc_validator.audience` is mandatory and is checked as an exact JWT
  audience before a token can be authorized.
- Microsoft Entra v2 issuers ending in `/v2.0` are validated exactly instead of
  being rewritten to the legacy `sts.windows.net` issuer.

The upstream project and its bundled `jwt-cpp` dependency are Apache-2.0
licensed, and their license texts are included in the image. PostgreSQL uses
the PostgreSQL License. No upstream credentials, packages, or mutable release
artifacts are copied into this repository.

Build and inspect locally with Colima:

```bash
make test-postgres-oidc-image
```

Do not configure PostgreSQL OAuth without a non-empty audience. PostgreSQL 18
does not ship a built-in validator; this extension is part of the authentication
security boundary and must be reviewed whenever its pinned commit changes.

## Red Hat PostgreSQL compatibility candidate (KAN-137)

`Dockerfile.rhel` is a separate evaluation build. It does not change the
production image, Kubernetes manifests, or the existing image-publishing job.
It uses the Red Hat PostgreSQL 18 image digest inspected on Arsene, whose
PostgreSQL server reports 18.6. Run the candidate with rootless Podman on Arsene.

On 2026-10-17, upstream Percona `HEAD` and tag `1.1.0` both resolved to
`ba9c4cb2dc9bd2e9779c7a33f7a30c9bb8c97862`, the commit already pinned here.
The pinned implementation has neither our mandatory audience setting nor our
Entra v2 issuer correction. Both existing patch sections remain required.
The upstream repository now lives at
<https://github.com/percona/pg_oidc_validator>; the candidate uses that URL.

The build requests matching Red Hat `postgresql18-server-devel` headers and
compiles the validator from source against them. It uses dynamic libstdc++
linking instead of upstream's static C++ library default. The runtime stage
checks for missing shared-library dependencies and retains Red Hat's
entrypoint and UID 26. Availability of build packages through the registered
host's repositories still needs a real build; registry login alone does not
establish package repository entitlement or support for the custom extension.

From a checkout on Arsene:

```sh
bash scripts/test_postgres_oidc_rhel.sh
```

Alternatively, copy `Dockerfile.rhel`, the `patches/` directory, and the test
script to an isolated folder, then pass that folder as the script's first
argument. Only that folder is sent as the build context; do not copy `.env`
files or backend credentials into it.

The probe starts one temporary container with networking disabled, no host
mounts or published ports, a 1 GiB memory limit, and a two-CPU limit. It checks
Red Hat initialization, actual validator preloading, configured settings, and
a password-authenticated test connection. The container is removed on exit;
the candidate image and build cache remain for inspection. No production or
development database volumes are touched.

This is a compatibility smoke test, not an Entra authentication test. Before
promotion, test valid Entra tokens, incorrect audience/issuer/signature,
expired tokens, missing scope/identity, and database role mapping. Also test
the fail-closed behavior with no configured audience, persistence/restart,
backup/restore, application schema bootstrap, and upgrade rollback.
Do not reuse an existing PostgreSQL data volume: Red Hat uses
`/var/lib/pgsql/data` and `POSTGRESQL_*` environment variables, unlike the
current image. Confirm vendor support coverage separately.

### Dedicated Entra validation registrations

Inspection of the existing BrownRook Ledger application found no exposed API
identifier or delegated scopes, and no public-client configuration. Do not
modify that web-login application to make this database test work.

`scripts/prepare_postgres_entra_validation.py` previews, and with `--apply`
creates, a dedicated single-tenant API and public sign-in client for the
Brown Rook tenant and operator identity inspected during KAN-137. The API
requests v2 access tokens and exposes only the delegated `db.connect` scope.
No client secrets, production database privileges, or tenant-wide consent
grants are created. Tenant consent policies can still require administrator
approval when requesting a token.

Run from the Mac checkout with an existing Azure CLI login:

```sh
.venv/bin/python scripts/prepare_postgres_entra_validation.py
.venv/bin/python scripts/prepare_postgres_entra_validation.py --apply
```

The helper checks tenant/operator identity before writes, marks and records
its own applications, refuses to patch the Ledger registration, and saves
object IDs and the resulting test configuration under ignored
`.validation/kan137/entra/`. Keep `registrations.json` until the dedicated test
registrations and service principals have been removed. Configuration success
does not establish token consent, database role mapping, or a successful SSO
login. Those need the subsequent isolated authentication tests.

### Actual Entra authentication probe

`scripts/postgres_entra_probe.py` uses only Python's standard library. Copy it
and the generated `config.json` to
`~/.local/state/brownrook/kan137/postgres-rhel/` on Arsene. Run `start` there:

```sh
python3 ~/.local/state/brownrook/kan137/postgres-rhel/postgres_entra_probe.py \
  --config ~/.local/state/brownrook/kan137/postgres-rhel/config.json start
```

This starts `brownrook-pg-entra-validation` using the locally tested candidate
image. It publishes only Arsene loopback port 15433, has no host volume mounts,
and can reach Microsoft discovery/JWKS endpoints outbound. It maps only the
configured operator's Entra object ID to an unprivileged `entra_probe` role.
It installs OAuth-only TCP rules for `probe_db`; local Unix socket access is
reserved for administering the temporary test container.

On the Mac, run these separately in order:

```sh
.venv/bin/python scripts/postgres_entra_probe.py \
  --config .validation/kan137/entra/config.json login
.venv/bin/python scripts/postgres_entra_probe.py \
  --config .validation/kan137/entra/config.json check
```

`login` uses Microsoft's device authorization flow. Sign in as the configured
operator and approve the dedicated validation scope if tenant policy permits.
If administrator approval is required, do not change tenant-wide policies to
make the test pass. The short-lived access token is saved in a mode-0600 file
on the Mac and is never printed or copied to Arsene.

`check` opens and closes its own SSH tunnel, performs a real PostgreSQL
OAUTHBEARER exchange and identity query, and checks rejection of an unmapped
role, modified signature, wrong audience, missing audience configuration,
and missing required scope. It changes policy only in the labeled temporary
container, restores the baseline in a finally block after each policy test,
and confirms a valid login again after each restoration. Network failures,
internal server errors, and password/trust fallback do not count as successful
authentication/rejection. Token payload inspection by the test client is only
a sanity check; signature verification is the database validator's job.

This test client implements only the subset of PostgreSQL's wire protocol
needed by the probe; it is not an application driver. A passed report does not
establish pgAdmin/libpq device-flow support. Validly signed expired tokens and
tokens from another issuer need separate fixtures; modifying an expiry/issuer
inside a JWT only tests signature rejection and is not claimed as expiry/issuer
coverage. Persistence, recovery and vendor support also remain separate checks.
Results without tokens are saved to `.validation/kan137/entra/auth-report.json`.

After testing, remove the temporary container on Arsene with the same command
used for startup but the `stop` subcommand. Delete
`.validation/kan137/entra/access-token.private` on the Mac when no longer
needed. Keep the registration IDs until the dedicated Entra registrations
have been reviewed for retention or removed.
