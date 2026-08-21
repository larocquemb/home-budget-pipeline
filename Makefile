TEST_DB ?= home_budget_test
TEST_DATABASE_URL ?= postgresql://localhost/$(TEST_DB)
RECEIPT_TEST_SOURCE ?= receipts
RECEIPT_TEST_ROOT ?= .receipt-test
RECEIPT_TEST_WORKERS ?= 2

.PHONY: test test-db-setup test-db test-db-verbose test-all test-receipts

test:
	pytest -q -m "not integration"

test-db-setup:
	@echo "Resetting PostgreSQL test database..."
	@psql postgres -Atqc "SELECT 1 FROM pg_database WHERE datname='$(TEST_DB)'" | grep -q 1 || createdb $(TEST_DB)
	@PGOPTIONS='--client-min-messages=warning' psql $(TEST_DATABASE_URL) -v ON_ERROR_STOP=1 -q -c 'DROP SCHEMA IF EXISTS budget CASCADE;' >/dev/null
	@echo "Loading schema..."
	@PGOPTIONS='--client-min-messages=warning' psql $(TEST_DATABASE_URL) -v ON_ERROR_STOP=1 -q -f sql/schema_phase1.sql >/dev/null
	@PGOPTIONS='--client-min-messages=warning' psql $(TEST_DATABASE_URL) -v ON_ERROR_STOP=1 -q -f sql/receipt_processing.sql >/dev/null

test-db: test-db-setup
	@echo "Running PostgreSQL integration tests..."
	@TEST_DATABASE_URL=$(TEST_DATABASE_URL) pytest -q -m integration

test-db-verbose:
	@psql postgres -Atqc "SELECT 1 FROM pg_database WHERE datname='$(TEST_DB)'" | grep -q 1 || createdb $(TEST_DB)
	psql $(TEST_DATABASE_URL) -v ON_ERROR_STOP=1 -c 'DROP SCHEMA IF EXISTS budget CASCADE;'
	psql $(TEST_DATABASE_URL) -v ON_ERROR_STOP=1 -f sql/schema_phase1.sql
	psql $(TEST_DATABASE_URL) -v ON_ERROR_STOP=1 -f sql/receipt_processing.sql
	TEST_DATABASE_URL=$(TEST_DATABASE_URL) pytest -q -m integration

test-all: test
	@$(MAKE) test-db

test-receipts: test-db-setup
	@test -d "$(RECEIPT_TEST_SOURCE)" || (echo "Receipt source not found: $(RECEIPT_TEST_SOURCE)"; echo "Set RECEIPT_TEST_SOURCE=/path/to/receipts"; exit 2)
	@echo "Preparing isolated receipt workspace from $(RECEIPT_TEST_SOURCE)..."
	@rm -rf "$(RECEIPT_TEST_ROOT)"
	@mkdir -p "$(RECEIPT_TEST_ROOT)/inbox" "$(RECEIPT_TEST_ROOT)/ocr-cache"
	@rsync -a "$(RECEIPT_TEST_SOURCE)/" "$(RECEIPT_TEST_ROOT)/inbox/"
	@echo "Running receipt backlog against local PostgreSQL test database..."
	@DATABASE_URL=$(TEST_DATABASE_URL) HOME_BUDGET_OCR_CACHE="$(RECEIPT_TEST_ROOT)/ocr-cache" \
		home-budget-process-receipts "$(RECEIPT_TEST_ROOT)/inbox" --workers $(RECEIPT_TEST_WORKERS)
