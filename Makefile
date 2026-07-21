.PHONY: help sync up down demo test lint typecheck dashboard validate-registry

help:
	@echo "sync             - uv sync (install deps + create .venv)"
	@echo "up               - start postgres + minio + dagster (docker compose)"
	@echo "down             - stop services"
	@echo "demo             - run the Phase-1 A1 walking skeleton end-to-end"
	@echo "test             - pytest (incl. guardrail suite)"
	@echo "lint             - ruff check + format --check"
	@echo "typecheck        - mypy"
	@echo "validate-registry- load + validate sources/*.yml"
	@echo "dashboard        - build the read-only tier-a dashboard"

sync:
	uv sync --extra dev

up:
	docker compose up -d postgres minio dagster

down:
	docker compose down

demo:
	uv run dagster asset materialize --select '*' -m uncorrupt.pipelines.definitions

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run mypy

validate-registry:
	uv run uncorrupt validate-registry

dashboard:
	cd dashboard && npm install && npm run build
