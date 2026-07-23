.PHONY: help sync up down migrate test lint typecheck validate-registry experiment

help:
	@echo "sync             - uv sync (install deps + create .venv)"
	@echo "up               - start postgres (docker compose)"
	@echo "down             - stop services"
	@echo "migrate          - run Django migrations"
	@echo "test             - pytest (incl. guardrail suite)"
	@echo "lint             - ruff check + format --check"
	@echo "typecheck        - mypy"
	@echo "validate-registry- load + validate sources/*.yml"
	@echo "experiment       - run the three-country kill experiment"

sync:
	uv sync --extra dev

up:
	docker compose up -d postgres

down:
	docker compose down

migrate:
	DJANGO_SETTINGS_MODULE=config.settings.dev uv run python manage.py migrate

test:
	DJANGO_SETTINGS_MODULE=config.settings.test uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run mypy

validate-registry:
	uv run uncorrupt validate-registry

experiment:
	DJANGO_SETTINGS_MODULE=config.settings.dev uv run python scripts/kill_experiment.py --output experiments/flags_raw.json
	DJANGO_SETTINGS_MODULE=config.settings.dev uv run python scripts/curate_flags.py --input experiments/flags_raw.json --top 10