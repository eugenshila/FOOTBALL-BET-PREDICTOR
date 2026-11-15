.PHONY: install install-pg run run-sqlite test test-pg check-db check-providers ui-test cards cli import load-stock reset-db export clean

install:
	python3 -m venv .venv
	./.venv/bin/pip install -r requirements.txt

# psycopg is already in requirements.txt; keep this if you installed before that
install-pg:
	./.venv/bin/pip install -r requirements-postgres.txt

# PostgreSQL is the default engine - .env supplies DATABASE_URL or POSTGRES_*
run:
	./.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# Same app on the file-backed engine, no database server needed
run-sqlite:
	DB_ENGINE=sqlite ./.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# The same suite runs on either engine. PostgreSQL:
#   make test-pg TEST_DATABASE_URL=postgresql://user:pw@localhost:5432/partsid_test
test:
	./.venv/bin/python -m pytest tests -q

test-pg:
	TEST_DATABASE_URL="$(TEST_DATABASE_URL)" ./.venv/bin/python -m pytest tests -q

check-db:
	./.venv/bin/python -m app.cli check-db

check-providers:
	./.venv/bin/python -m app.cli check-providers

# Browser-level regression pass; needs Node + `npm install jsdom`
ui-test:
	node scripts/ui_smoke.cjs

# Replace the whole inventory from a spreadsheet (validates before deleting)
load-stock:
	./.venv/bin/python -m app.cli load-stock $(CSV) --replace

reset-db:
	./.venv/bin/python -m app.cli reset-db

# Render the sample promo cards into docs/samples
cards:
	./.venv/bin/python scripts/render_samples.py

cli:
	./.venv/bin/python -m app.cli list

# Dry-run a stock list first: make import CSV=stock.csv
import:
	./.venv/bin/python -m app.cli import-csv $(CSV)

export:
	./.venv/bin/python -m app.cli export-csv --out inventory.csv

clean:
	rm -rf __pycache__ app/__pycache__ tests/__pycache__ .pytest_cache uploads/* exports/*
	rm -f data/inventory.db data/feature_cache.json
