TEST_DB ?= home_budget_test
TEST_DATABASE_URL ?= postgresql://localhost/$(TEST_DB)
RECEIPT_TEST_ROOT ?= .receipt-test
RECEIPT_TEST_WORKERS ?= 6
RECEIPT_SOURCE_ROOT ?= $(HOME_BUDGET_DATA_ROOT)/receipts/raw/scanned/inbox
KUBE_NAMESPACE ?= home-budget
ENRICH_JOB ?= product-enrichment-manual
ENRICH_LIMIT ?= 100
ENRICH_THRESHOLD ?= 0.85
ITEM_ID ?=
RECEIPT ?=
MERCHANT ?=

.PHONY: test test-db-setup test-db test-db-verbose test-all test-receipts status dev-up dev-down dev-web dev-cert-install dev-db-reset enrich-products

status:
	@./scripts/deployment_status.sh

dev-up:
	@test -f .env.dev || (echo "Copy .env.dev.example to .env.dev and fill in its values"; exit 2)
	docker-compose --env-file .env.dev -f compose.dev.yaml up -d

dev-down:
	docker-compose --env-file .env.dev -f compose.dev.yaml down

dev-web:
	@test -f .env.dev || (echo "Copy .env.dev.example to .env.dev and fill in its values"; exit 2)
	@set -a; . ./.env.dev; set +a; \
		LEDGER_BASE_PATH=/ledger HOST=0.0.0.0 PORT=8080 \
		.venv/bin/uvicorn home_budget_pipeline.web.app:app \
			--host 0.0.0.0 --port 8080 --reload

dev-db-reset:
	@./scripts/rebuild_local_database.sh .env.dev

dev-cert-install:
	@mkdir -p .dev-certs
	docker-compose --env-file .env.dev -f compose.dev.yaml cp \
		caddy:/data/caddy/pki/authorities/local/root.crt .dev-certs/caddy-root.crt
	sudo security add-trusted-cert -d -r trustRoot \
		-k /Library/Keychains/System.keychain .dev-certs/caddy-root.crt

enrich-products:
	@set -eu; \
	if [ -n "$(ITEM_ID)" ] && [ -n "$(RECEIPT)" ]; then echo "Set ITEM_ID or RECEIPT, not both"; exit 2; fi; \
	for secret in postgres-secret brave-search-api openai-api ghcr-secret; do \
		kubectl -n "$(KUBE_NAMESPACE)" get secret "$$secret" >/dev/null || { echo "Missing Kubernetes secret: $$secret"; exit 2; }; \
	done; \
	IMAGE=$$(kubectl -n "$(KUBE_NAMESPACE)" get deployment ledger-web -o jsonpath='{.spec.template.spec.containers[0].image}'); \
	test -n "$$IMAGE" || { echo "Could not determine ledger-web image"; exit 2; }; \
	kubectl -n "$(KUBE_NAMESPACE)" delete job "$(ENRICH_JOB)" --ignore-not-found >/dev/null; \
	{ \
		echo 'apiVersion: batch/v1'; \
		echo 'kind: Job'; \
		echo 'metadata:'; \
		echo '  name: $(ENRICH_JOB)'; \
		echo 'spec:'; \
		echo '  backoffLimit: 0'; \
		echo '  template:'; \
		echo '    spec:'; \
		echo '      restartPolicy: Never'; \
		echo '      imagePullSecrets:'; \
		echo '        - name: ghcr-secret'; \
		echo '      containers:'; \
		echo '        - name: enrich'; \
		echo "          image: $$IMAGE"; \
		echo '          command:'; \
		if [ -n "$(RECEIPT)" ]; then \
			echo '            - python'; \
			echo '          args:'; \
			echo '            - -m'; \
			echo '            - home_budget_pipeline.receipt_enrichment'; \
			echo '            - --receipt'; \
			echo '            - "$(RECEIPT)"'; \
			if [ -n "$(MERCHANT)" ]; then echo '            - --merchant'; echo '            - "$(MERCHANT)"'; fi; \
		else \
			echo '            - home-budget-enrich-products'; \
			echo '          args:'; \
			if [ -n "$(ITEM_ID)" ]; then echo '            - --item-id'; echo '            - "$(ITEM_ID)"'; fi; \
		fi; \
		echo '            - --limit'; \
		echo '            - "$(ENRICH_LIMIT)"'; \
		echo '            - --threshold'; \
		echo '            - "$(ENRICH_THRESHOLD)"'; \
		echo '            - --write-db'; \
		echo '          env:'; \
		echo '            - name: DATABASE_URL'; \
		echo '              valueFrom:'; \
		echo '                secretKeyRef:'; \
		echo '                  name: postgres-secret'; \
		echo '                  key: DATABASE_URL'; \
		echo '            - name: BRAVE_SEARCH_API_KEY'; \
		echo '              valueFrom:'; \
		echo '                secretKeyRef:'; \
		echo '                  name: brave-search-api'; \
		echo '                  key: BRAVE_SEARCH_API_KEY'; \
		echo '            - name: OPENAI_API_KEY'; \
		echo '              valueFrom:'; \
		echo '                secretKeyRef:'; \
		echo '                  name: openai-api'; \
		echo '                  key: OPENAI_API_KEY'; \
	} | kubectl -n "$(KUBE_NAMESPACE)" apply -f - >/dev/null; \
	echo "Started $(ENRICH_JOB) using $$IMAGE"; \
	if [ -n "$(RECEIPT)" ]; then echo "Scope: receipt $(RECEIPT)$${MERCHANT:+, merchant $(MERCHANT)}"; elif [ -n "$(ITEM_ID)" ]; then echo "Scope: item $(ITEM_ID)"; else echo "Scope: pending backlog (limit $(ENRICH_LIMIT))"; fi; \
	kubectl -n "$(KUBE_NAMESPACE)" wait --for=condition=Ready pod -l job-name="$(ENRICH_JOB)" --timeout=120s >/dev/null 2>&1 || true; \
	kubectl -n "$(KUBE_NAMESPACE)" logs -f job/"$(ENRICH_JOB)"

test:
	pytest -q -m "not integration"

test-db-setup:
	@echo "Resetting PostgreSQL test database..."
	@psql postgres -Atqc "SELECT 1 FROM pg_database WHERE datname='$(TEST_DB)'" | grep -q 1 || createdb $(TEST_DB)
	@PGOPTIONS='--client-min-messages=warning' psql $(TEST_DATABASE_URL) -v ON_ERROR_STOP=1 -q -c 'DROP SCHEMA IF EXISTS ingest_blue CASCADE; DROP SCHEMA IF EXISTS ingest_green CASCADE; DROP SCHEMA IF EXISTS ingest CASCADE; DROP SCHEMA IF EXISTS ops CASCADE; DROP SCHEMA IF EXISTS budget CASCADE;' >/dev/null
	@echo "Loading schema..."
	@PGOPTIONS='--client-min-messages=warning' psql $(TEST_DATABASE_URL) -v ON_ERROR_STOP=1 -q -f sql/schema_phase1.sql >/dev/null
	@PGOPTIONS='--client-min-messages=warning' psql $(TEST_DATABASE_URL) -v ON_ERROR_STOP=1 -q -f sql/schema_constraints.sql >/dev/null
	@PGOPTIONS='--client-min-messages=warning' psql $(TEST_DATABASE_URL) -v ON_ERROR_STOP=1 -q -f sql/receipt_processing.sql >/dev/null

test-db: test-db-setup
	@echo "Running PostgreSQL integration tests..."
	@TEST_DATABASE_URL=$(TEST_DATABASE_URL) pytest -q -m integration

test-db-verbose:
	@psql postgres -Atqc "SELECT 1 FROM pg_database WHERE datname='$(TEST_DB)'" | grep -q 1 || createdb $(TEST_DB)
	psql $(TEST_DATABASE_URL) -v ON_ERROR_STOP=1 -c 'DROP SCHEMA IF EXISTS ingest_blue CASCADE; DROP SCHEMA IF EXISTS ingest_green CASCADE; DROP SCHEMA IF EXISTS ingest CASCADE; DROP SCHEMA IF EXISTS ops CASCADE; DROP SCHEMA IF EXISTS budget CASCADE;'
	psql $(TEST_DATABASE_URL) -v ON_ERROR_STOP=1 -f sql/schema_phase1.sql
	psql $(TEST_DATABASE_URL) -v ON_ERROR_STOP=1 -f sql/schema_constraints.sql
	psql $(TEST_DATABASE_URL) -v ON_ERROR_STOP=1 -f sql/receipt_processing.sql
	TEST_DATABASE_URL=$(TEST_DATABASE_URL) pytest -q -m integration

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
		python -m home_budget_pipeline.receipts.backlog_ingest "$(RECEIPT_TEST_ROOT)/inbox" --workers $(RECEIPT_TEST_WORKERS)
