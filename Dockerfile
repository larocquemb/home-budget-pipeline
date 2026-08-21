ARG OCR_BASE_IMAGE=ghcr.io/larocquemb/home-budget-ocr-base:rhel10.2

FROM ${OCR_BASE_IMAGE}

LABEL org.opencontainers.image.source="https://github.com/larocquemb/home-budget-pipeline"

ENV HOME_BUDGET_DATA_ROOT=/data \
    HOME_BUDGET_SQL_DIR=/opt/app-root/src/sql

USER 0

WORKDIR /opt/app-root/src

COPY pyproject.toml README.md ./

# Install third-party dependencies before copying application source so this
# expensive layer remains cached for source-only changes. The temporary package
# lets pip resolve the canonical dependency metadata directly from pyproject.
RUN mkdir -p src/home_budget_pipeline \
    && touch src/home_budget_pipeline/__init__.py \
    && python -m pip install --no-cache-dir 'setuptools>=77' \
    && python -m pip install --no-cache-dir --no-build-isolation '.[db]'

COPY src ./src
COPY sql ./sql
COPY config ./config

RUN python -m pip install --no-cache-dir --no-deps --no-build-isolation --force-reinstall . \
    && mkdir -p /data \
    && chown -R 1001:0 /data /opt/app-root/src \
    && chmod -R g=u /data /opt/app-root/src

USER 1001

RUN tesseract --version \
    && tesseract --list-langs | grep -qx eng

ENTRYPOINT ["python", "-m"]
CMD ["home_budget_pipeline.receipts.parallel_ingest", "/data/receipts/raw/scanned/inbox"]
