ARG OCR_BASE_IMAGE=ghcr.io/larocquemb/home-budget-ocr-base:rhel10.2

FROM ${OCR_BASE_IMAGE}

LABEL org.opencontainers.image.source="https://github.com/larocquemb/home-budget-pipeline"

ENV HOME_BUDGET_DATA_ROOT=/data \
    HOME_BUDGET_SQL_DIR=/opt/app-root/src/sql \
    PADDLE_PDX_CACHE_HOME=/opt/paddlex-cache \
    PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \
    PYTHONWARNINGS="ignore:No ccache found.*:UserWarning"

USER 0

WORKDIR /opt/app-root/src

# Paddle's CPU wheel links against GNU OpenMP, while PaddleX's required OpenCV
# wheel links against libGL. The slim OCR base omits both runtime libraries.
RUN microdnf install -y libgomp mesa-libGL \
    && microdnf clean all

COPY pyproject.toml README.md ./

# Install third-party dependencies before copying application source so this
# expensive layer remains cached for source-only changes. The temporary package
# lets pip resolve the canonical dependency metadata directly from pyproject.
RUN mkdir -p src/home_budget_pipeline \
    && touch src/home_budget_pipeline/__init__.py \
    && python -m pip install --no-cache-dir 'setuptools>=77' \
    && python -m pip install --no-cache-dir --no-build-isolation '.[db,paddle]'

COPY src ./src
COPY sql ./sql
COPY config ./config
COPY scripts/stage_db_bootstrap.sh ./scripts/stage_db_bootstrap.sh

RUN python -m pip install --no-cache-dir --no-deps --no-build-isolation --force-reinstall . \
    && mkdir -p /data /opt/paddlex-cache \
    && chown -R 1001:0 /data /opt/app-root/src /opt/paddlex-cache \
    && chmod -R g=u /data /opt/app-root/src /opt/paddlex-cache

USER 1001

RUN tesseract --version \
    && tesseract --list-langs | grep -qx eng \
    && python -c "from paddleocr import PaddleOCR; PaddleOCR(text_detection_model_name='PP-OCRv6_medium_det', text_recognition_model_name='PP-OCRv6_medium_rec', use_doc_orientation_classify=False, use_doc_unwarping=False, use_textline_orientation=False)"

ENTRYPOINT ["python", "-m"]
CMD ["home_budget_pipeline.receipts.queue_ingest", "publish", "/data/receipts/raw/scanned/inbox"]
