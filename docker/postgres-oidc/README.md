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
