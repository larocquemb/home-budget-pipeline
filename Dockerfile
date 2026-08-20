FROM registry.access.redhat.com/ubi10/python-312-minimal:10.2

LABEL org.opencontainers.image.source="https://github.com/larocquemb/home-budget-pipeline"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME_BUDGET_DATA_ROOT=/data

USER 0

RUN microdnf install -y tesseract \
    && microdnf clean all

WORKDIR /opt/app-root/src

COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m pip install --no-cache-dir . \
    && mkdir -p /data \
    && chown -R 1001:0 /data \
    && chmod -R g=u /data

USER 1001

ENTRYPOINT ["python", "-m"]
CMD ["home_budget_pipeline.receipts.parallel_ingest", "/data/receipts/raw/scanned/inbox"]
