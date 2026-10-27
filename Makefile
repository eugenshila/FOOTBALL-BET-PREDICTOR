.PHONY: install install-pg run run-pg test test-pg check-db check-providers cards cli import export clean

install:
	python3 -m venv .venv
	./.venv/bin/pip install -r requirements.txt

# only needed when DATABASE_URL points at PostgreSQL
install-pg:
	./.venv/bin/pip install -r requirements-postgres.txt

run:
	./.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

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
