TEST_DB ?= home_budget_test
TEST_DATABASE_URL ?= postgresql://localhost/$(TEST_DB)
RECEIPT_TEST_ROOT ?= .receipt-test
RECEIPT_TEST_WORKERS ?= 6
RECEIPT_SOURCE_ROOT ?= $(HOME_BUDGET_DATA_ROOT)/receipts/raw/scanned/inbox

.PHONY: test test-db-setup test-db test-db-verbose test-all test-receipts status

status:
	@./scripts/deployment_status.sh

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
