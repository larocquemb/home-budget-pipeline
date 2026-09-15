# syntax=docker/dockerfile:1.7

ARG POSTGRES_IMAGE=postgres:18.6-bookworm@sha256:1c59e2c3c818eaa0f0628f695b36e7c9e362d6b219b36a54a32df645cbd7e1af

FROM ${POSTGRES_IMAGE} AS validator-build

ARG PG_OIDC_VALIDATOR_REPOSITORY=https://github.com/Percona-Lab/pg_oidc_validator.git
ARG PG_OIDC_VALIDATOR_COMMIT=ba9c4cb2dc9bd2e9779c7a33f7a30c9bb8c97862

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        g++ \
        git \
        libcurl4-openssl-dev \
        libkrb5-dev \
        make \
        postgresql-server-dev-18 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src/pg_oidc_validator

RUN git clone --recursive "${PG_OIDC_VALIDATOR_REPOSITORY}" . \
    && git checkout --detach "${PG_OIDC_VALIDATOR_COMMIT}" \
    && git submodule update --init --recursive \
    && test "$(git rev-parse HEAD)" = "${PG_OIDC_VALIDATOR_COMMIT}"

COPY docker/postgres-oidc/patches/0001-require-exact-audience-and-support-entra-v2.patch /tmp/validator.patch

RUN git apply --check /tmp/validator.patch \
    && git apply /tmp/validator.patch \
    && make USE_PGXS=1 -j"$(nproc)" \
    && make USE_PGXS=1 DESTDIR=/opt/pg-oidc-validator install \
    && install -D -m 0644 LICENSE.txt /opt/pg-oidc-validator/usr/share/doc/pg_oidc_validator/LICENSE.txt \
    && install -D -m 0644 jwt-cpp/LICENSE /opt/pg-oidc-validator/usr/share/doc/pg_oidc_validator/LICENSE.jwt-cpp

FROM ${POSTGRES_IMAGE}

ARG PG_OIDC_VALIDATOR_COMMIT=ba9c4cb2dc9bd2e9779c7a33f7a30c9bb8c97862

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libcurl4 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=validator-build /opt/pg-oidc-validator/ /

LABEL org.opencontainers.image.source="https://github.com/larocquemb/home-budget-pipeline" \
      org.opencontainers.image.title="Home Budget PostgreSQL 18 with OAuth validator" \
      org.opencontainers.image.description="PostgreSQL 18 with a pinned and audience-hardened pg_oidc_validator" \
      org.opencontainers.image.licenses="PostgreSQL AND Apache-2.0" \
      org.opencontainers.image.vendor="Brown Rook" \
      io.brownrook.pg_oidc_validator.commit="${PG_OIDC_VALIDATOR_COMMIT}"

RUN postgres --version \
    && test -f "$(pg_config --pkglibdir)/pg_oidc_validator.so"
