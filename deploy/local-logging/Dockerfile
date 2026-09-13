FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LEDGER_BASE_PATH=/ledger

WORKDIR /app

COPY pyproject.toml README.md ./

# Cache the third-party dependencies separately from application source.
RUN mkdir -p src/home_budget_pipeline \
    && touch src/home_budget_pipeline/__init__.py \
    && python -m pip install --no-cache-dir "setuptools>=77" \
    && python -m pip install --no-cache-dir --no-build-isolation .

COPY src ./src

RUN python -m pip install --no-cache-dir --no-deps --no-build-isolation --force-reinstall . \
    && useradd --create-home --uid 10001 homebudget \
    && chown -R homebudget:homebudget /app

USER 10001

EXPOSE 8080

CMD ["uvicorn", "home_budget_pipeline.web.receipt_app:app", "--host", "0.0.0.0", "--port", "8080", "--access-log"]
