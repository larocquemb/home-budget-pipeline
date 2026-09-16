TEST_DB ?= home_budget_test
TEST_DATABASE_URL ?= postgresql://localhost/$(TEST_DB)
PYTHON ?= .venv/bin/python
LEDGER ?= .venv/bin/ledger
MKDOCS ?= .venv/bin/mkdocs
RECEIPT_TEST_ROOT ?= .receipt-test
RECEIPT_TEST_WORKERS ?= 6
RECEIPT_SOURCE_ROOT ?= $(HOME_BUDGET_DATA_ROOT)/receipts/raw/scanned/inbox
KUBE_NAMESPACE ?= home-budget
ARGO_APP ?= ledger
ARGO_SERVER ?= argocd.brownrook.net
export ARGO_APP ARGO_SERVER
ENRICH_JOB ?= product-enrichment-manual-$(shell date +%s)
MONITORING_GITOPS ?= ./scripts/monitoring_gitops.sh
LOG_COLLECTION_COMPARE ?= ./scripts/compare_log_collection_probe.sh

.PHONY: help docs-build docs-serve test test-observability test-postgres-oidc-image test-db-setup test-db test-db-verbose test-rabbit test-all test-receipts status otlp-demo dev-up dev-down dev-web dev-cert-install dev-db-reset dev-logging-up dev-logging-test dev-logging-status dev-logging-logs dev-logging-down enrich-products postgres-config-check postgres-config-apply postgres-password-rotate deploy-k3s k3s-config-check k3s-config-apply monitoring-gitops-syntax monitoring-gitops-check monitoring-gitops-apply log-collection-compare

help:
	@echo "Development: dev-up dev-down dev-web dev-cert-install dev-db-reset"
	@echo "Local logs:  dev-logging-up dev-logging-test dev-logging-status dev-logging-logs dev-logging-down"
	@echo "Documentation: docs-build docs-serve"
	@echo "Tests:       test test-observability test-postgres-oidc-image test-db-setup test-db test-db-verbose test-rabbit test-all test-receipts"
	@echo "Operations:  status otlp-demo enrich-products"
	@echo "Deployment:  deploy-k3s k3s-config-check k3s-config-apply (uses .env.k3s)"
	@echo "PostgreSQL:  postgres-config-check postgres-config-apply postgres-password-rotate"
	@echo "Monitoring:  monitoring-gitops-syntax monitoring-gitops-check monitoring-gitops-apply log-collection-compare"

docs-build:
	$(MKDOCS) build --strict

docs-serve:
	$(MKDOCS) serve

status:
	@./scripts/deployment_status.sh

otlp-demo:
	@./scripts/demo_otlp.sh

postgres-config-check:
	@$(PYTHON) scripts/postgres_credentials.py

postgres-config-apply:
	@$(PYTHON) scripts/postgres_credentials.py --apply

postgres-password-rotate:
	@$(PYTHON) scripts/postgres_credentials.py --rotate

k3s-config-check:
	@$(PYTHON) scripts/k3s_runtime_config.py

k3s-config-apply:
	@$(PYTHON) scripts/k3s_runtime_config.py --apply

monitoring-gitops-syntax:
	@$(MONITORING_GITOPS) syntax

monitoring-gitops-check:
	@$(MONITORING_GITOPS) check

monitoring-gitops-apply:
	@$(MONITORING_GITOPS) apply

log-collection-compare:
	@test -n "$(MARKER)" || (echo "Usage: make log-collection-compare MARKER=NAME [SINCE=1h]" >&2; exit 2)
	@$(LOG_COLLECTION_COMPARE) "$(MARKER)" "$(if $(SINCE),$(SINCE),1h)"

deploy-k3s: postgres-config-check k3s-config-check
	@argocd app get "$(ARGO_APP)" --server "$(ARGO_SERVER)" --grpc-web >/dev/null
	@$(PYTHON) scripts/postgres_credentials.py --sync
	@$(PYTHON) scripts/k3s_runtime_config.py --apply
	argocd app sync "$(ARGO_APP)" --server "$(ARGO_SERVER)" --grpc-web

dev-up:
	@$(PYTHON) scripts/local_development_up.py .env.dev

dev-down:
	@./scripts/docker_compose.sh --env-file .env.dev -f compose.dev.yaml down

dev-web:
	@test -f .env.dev || (echo "Copy .env.dev.example to .env.dev and fill in its values"; exit 2)
	@test -f .local-postgres/runtime.env || (echo "Run make dev-up to initialize local PostgreSQL 18"; exit 2)
	@set -a; . ./.env.dev; . ./.local-postgres/runtime.env; set +a; \
		test -n "$$LEDGER_PROXY_SECRET" || { echo "Set LEDGER_PROXY_SECRET in .env.dev"; exit 2; }; \
		LEDGER_BASE_PATH=/ledger HOST=0.0.0.0 PORT=8080 \
		.venv/bin/uvicorn home_budget_pipeline.web.app:app \
			--host 0.0.0.0 --port 8080 --reload

dev-db-reset:
	@test -f .local-postgres/runtime.env || (echo "Run make dev-up to initialize local PostgreSQL 18"; exit 2)
	@./scripts/rebuild_local_database.sh .local-postgres/runtime.env

dev-cert-install:
	@mkdir -p .dev-certs
	docker-compose --env-file .env.dev -f compose.dev.yaml cp \
		caddy:/data/caddy/pki/authorities/local/root.crt .dev-certs/caddy-root.crt
	sudo security add-trusted-cert -d -r trustRoot \
		-k /Library/Keychains/System.keychain .dev-certs/caddy-root.crt

dev-logging-up:
	@./scripts/local_logging_up.sh

dev-logging-test:
	@./scripts/local_logging_test.sh

dev-logging-status:
	@kubectl --context kind-home-budget-logging -n home-budget get pods
	@./scripts/docker_compose.sh -p home-budget-local-logging \
		-f deploy/local-logging/compose.yaml ps

dev-logging-logs:
	./scripts/docker_compose.sh -p home-budget-local-logging \
		-f deploy/local-logging/compose.yaml logs -f alloy loki grafana

dev-logging-down:
	@./scripts/local_logging_down.sh

enrich-products:
	@set -eu; \
	kubectl -n "$(KUBE_NAMESPACE)" get cronjob product-enrichment >/dev/null || { echo "Missing CronJob: product-enrichment"; exit 2; }; \
	kubectl -n "$(KUBE_NAMESPACE)" create job --from=cronjob/product-enrichment "$(ENRICH_JOB)" >/dev/null; \
	echo "Started $(ENRICH_JOB) from CronJob/product-enrichment"; \
	kubectl -n "$(KUBE_NAMESPACE)" wait --for=condition=Ready pod -l job-name="$(ENRICH_JOB)" --timeout=120s >/dev/null 2>&1 || true; \
	kubectl -n "$(KUBE_NAMESPACE)" logs -f job/"$(ENRICH_JOB)"

test:
	$(PYTHON) -m pytest -q -m "not integration"

test-observability:
	@bash ./scripts/validate_observability_configs.sh

test-postgres-oidc-image:
	@bash ./scripts/test_postgres_oidc_image.sh

test-db-setup:
	@echo "Resetting PostgreSQL test database..."
	@psql postgres -Atqc "SELECT 1 FROM pg_database WHERE datname='$(TEST_DB)'" | grep -q 1 || createdb $(TEST_DB)
	@PGOPTIONS='--client-min-messages=warning' psql $(TEST_DATABASE_URL) -v ON_ERROR_STOP=1 -q -c 'DROP SCHEMA IF EXISTS ingest_blue CASCADE; DROP SCHEMA IF EXISTS ingest_green CASCADE; DROP SCHEMA IF EXISTS ingest CASCADE; DROP SCHEMA IF EXISTS ops CASCADE; DROP SCHEMA IF EXISTS budget CASCADE;' >/dev/null
	@echo "Loading schema..."
	@PGOPTIONS='--client-min-messages=warning' psql $(TEST_DATABASE_URL) -v ON_ERROR_STOP=1 -q -f sql/schema_phase1.sql >/dev/null
	@PGOPTIONS='--client-min-messages=warning' psql $(TEST_DATABASE_URL) -v ON_ERROR_STOP=1 -q -f sql/receipt_processing.sql >/dev/null
	@PGOPTIONS='--client-min-messages=warning' psql $(TEST_DATABASE_URL) -v ON_ERROR_STOP=1 -q -f sql/schema_constraints.sql >/dev/null
	@PGOPTIONS='--client-min-messages=warning' psql $(TEST_DATABASE_URL) -v ON_ERROR_STOP=1 -q -f sql/product_enrichment.sql >/dev/null
	@PGOPTIONS='--client-min-messages=warning' psql $(TEST_DATABASE_URL) -v ON_ERROR_STOP=1 -q -f sql/receipt_reprocess_requests.sql >/dev/null
	@PGOPTIONS='--client-min-messages=warning' psql $(TEST_DATABASE_URL) -v ON_ERROR_STOP=1 -q -f sql/ocr_result_collector.sql >/dev/null

test-db: test-db-setup
	@if test -n "$${TEST_RABBITMQ_URL:-$${RABBITMQ_URL:-}}"; then \
		echo "Running PostgreSQL and RabbitMQ integration tests..."; \
	else \
		echo "Running PostgreSQL integration tests; RabbitMQ tests will be skipped (no broker URL)..."; \
	fi
	@TEST_DATABASE_URL=$(TEST_DATABASE_URL) \
		TEST_RABBITMQ_URL="$${TEST_RABBITMQ_URL:-$${RABBITMQ_URL:-}}" \
		$(PYTHON) -m pytest -q -rA -m integration

test-db-verbose:
	@psql postgres -Atqc "SELECT 1 FROM pg_database WHERE datname='$(TEST_DB)'" | grep -q 1 || createdb $(TEST_DB)
	psql $(TEST_DATABASE_URL) -v ON_ERROR_STOP=1 -c 'DROP SCHEMA IF EXISTS ingest_blue CASCADE; DROP SCHEMA IF EXISTS ingest_green CASCADE; DROP SCHEMA IF EXISTS ingest CASCADE; DROP SCHEMA IF EXISTS ops CASCADE; DROP SCHEMA IF EXISTS budget CASCADE;'
	psql $(TEST_DATABASE_URL) -v ON_ERROR_STOP=1 -f sql/schema_phase1.sql
	psql $(TEST_DATABASE_URL) -v ON_ERROR_STOP=1 -f sql/receipt_processing.sql
	psql $(TEST_DATABASE_URL) -v ON_ERROR_STOP=1 -f sql/schema_constraints.sql
	psql $(TEST_DATABASE_URL) -v ON_ERROR_STOP=1 -f sql/product_enrichment.sql
	psql $(TEST_DATABASE_URL) -v ON_ERROR_STOP=1 -f sql/receipt_reprocess_requests.sql
	psql $(TEST_DATABASE_URL) -v ON_ERROR_STOP=1 -f sql/ocr_result_collector.sql
	TEST_DATABASE_URL=$(TEST_DATABASE_URL) \
		TEST_RABBITMQ_URL="$${TEST_RABBITMQ_URL:-$${RABBITMQ_URL:-}}" \
		$(PYTHON) -m pytest -q -m integration

test-rabbit: test-db-setup
	@test -n "$${TEST_RABBITMQ_URL:-$${RABBITMQ_URL:-}}" || { echo "Export RABBITMQ_URL or TEST_RABBITMQ_URL"; exit 2; }
	@TEST_DATABASE_URL=$(TEST_DATABASE_URL) \
		TEST_RABBITMQ_URL="$${TEST_RABBITMQ_URL:-$${RABBITMQ_URL:-}}" \
		$(PYTHON) -m pytest -v -m integration tests/test_receipt_queue_rabbitmq.py

test-all: test
	@$(MAKE) test-db

test-receipts: test-db-setup
	@test -n "$(HOME_BUDGET_DATA_ROOT)" || (echo "HOME_BUDGET_DATA_ROOT is not set"; exit 2)
	@test -d "$(RECEIPT_SOURCE_ROOT)" || (echo "Receipt source not found: $(RECEIPT_SOURCE_ROOT)"; exit 2)
	@echo "Preparing isolated receipt workspace from $(RECEIPT_SOURCE_ROOT)..."
	@rm -rf "$(RECEIPT_TEST_ROOT)"
	@mkdir -p "$(RECEIPT_TEST_ROOT)/inbox" "$(RECEIPT_TEST_ROOT)/ocr-cache"
	@rsync -a "$(RECEIPT_SOURCE_ROOT)/" "$(RECEIPT_TEST_ROOT)/inbox/"
	@echo "Running receipt backlog against local PostgreSQL test database..."
	@DATABASE_URL=$(TEST_DATABASE_URL) HOME_BUDGET_OCR_CACHE="$(RECEIPT_TEST_ROOT)/ocr-cache" \
		$(LEDGER) receipts process "$(RECEIPT_TEST_ROOT)/inbox" --workers $(RECEIPT_TEST_WORKERS)
