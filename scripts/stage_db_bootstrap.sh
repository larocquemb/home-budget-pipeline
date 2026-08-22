#!/bin/sh
set -eu

destination=${1:?usage: stage_db_bootstrap.sh DESTINATION}
repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

mkdir -p "$destination"
cp "$repo_root/sql/schema_phase1.sql" "$destination/001-schema.sql"
home-budget-category-catalog \
  "$repo_root/config/categories.yaml" \
  "$destination/002-categories.sql"
cp "$repo_root/sql/analytics_views.sql" "$destination/003-analytics-views.sql"
cp "$repo_root/sql/financial_transactions.sql" "$destination/004-financial-transactions.sql"
cp "$repo_root/sql/receipt_deduplication.sql" "$destination/005-receipt-deduplication.sql"
cp "$repo_root/sql/schema_constraints.sql" "$destination/006-schema-constraints.sql"
cp "$repo_root/sql/receipt_processing.sql" "$destination/007-receipt-processing.sql"
cp "$repo_root/sql/merchant_aliases.sql" "$destination/008-merchant-aliases.sql"
