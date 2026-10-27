.PHONY: install run test cards cli import export clean

install:
	python3 -m venv .venv
	./.venv/bin/pip install -r requirements.txt

run:
	./.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

test:
	./.venv/bin/python -m pytest tests -q

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
