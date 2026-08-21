TEST_DB ?= home_budget_test
TEST_DATABASE_URL ?= postgresql://localhost/$(TEST_DB)

.PHONY: test test-db-setup test-db test-db-verbose test-all

test:
	pytest -q -m "not integration"

test-db-setup:
	@echo "Resetting PostgreSQL test database..."
	@psql postgres -Atqc "SELECT 1 FROM pg_database WHERE datname='$(TEST_DB)'" | grep -q 1 || createdb $(TEST_DB)
	@psql $(TEST_DATABASE_URL) -v ON_ERROR_STOP=1 -q -c 'DROP SCHEMA IF EXISTS budget CASCADE;' >/dev/null
	@echo "Loading schema..."
	@psql $(TEST_DATABASE_URL) -v ON_ERROR_STOP=1 -q -f sql/schema_phase1.sql >/dev/null

test-db: test-db-setup
	@echo "Running PostgreSQL integration tests..."
	@TEST_DATABASE_URL=$(TEST_DATABASE_URL) pytest -q -m integration

test-db-verbose:
	@psql postgres -Atqc "SELECT 1 FROM pg_database WHERE datname='$(TEST_DB)'" | grep -q 1 || createdb $(TEST_DB)
	psql $(TEST_DATABASE_URL) -v ON_ERROR_STOP=1 -c 'DROP SCHEMA IF EXISTS budget CASCADE;'
	psql $(TEST_DATABASE_URL) -v ON_ERROR_STOP=1 -f sql/schema_phase1.sql
	TEST_DATABASE_URL=$(TEST_DATABASE_URL) pytest -q -m integration

test-all: test
	@$(MAKE) test-db
