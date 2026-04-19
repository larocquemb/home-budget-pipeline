# home-budget-pipeline

## Safe Postgres Auth

Avoid putting DB passwords in shell history or command arguments.

Use `~/.pgpass` (recommended):

```bash
cat > ~/.pgpass <<'EOF'
localhost:5432:home_budget:postgres:YOUR_PASSWORD
EOF
chmod 600 ~/.pgpass
```

Then run the loader without inline password:

```bash
.venv/bin/python3 instacart_orders_extract_persistent.py --write-db --db-dsn "host=localhost port=5432 dbname=home_budget user=postgres"
```

Alternative: set `HOME_BUDGET_PGPASSWORD` (script maps it to `PGPASSWORD` if needed), but `~/.pgpass` is preferred.
