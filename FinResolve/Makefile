# FinResolve AI — dev tasks (Linux/macOS/Git Bash). Windows PowerShell: scripts/dev.ps1
API_DIR := backend
WEB_DIR := frontend
VENV    := $(API_DIR)/.venv
PY      := $(VENV)/bin/python
COMPOSE := docker compose -f infra/docker-compose.yml

.PHONY: setup setup-api setup-web api worker web migrate test lint typecheck check web-check \
        openapi up down logs compose-config

setup: setup-api setup-web

setup-api:
	python3 -m venv $(VENV)
	$(PY) -m pip install --upgrade pip
	cd $(API_DIR) && .venv/bin/python -m pip install -e ".[dev]"

setup-web:
	cd $(WEB_DIR) && npm ci

api:
	cd $(API_DIR) && .venv/bin/uvicorn app.main:app --reload --port 8000

worker:
	cd $(API_DIR) && .venv/bin/python -m app.supervisor.worker

web:
	cd $(WEB_DIR) && npm run dev

migrate:
	cd $(API_DIR) && .venv/bin/alembic upgrade head

test:
	cd $(API_DIR) && .venv/bin/python -m pytest

lint:
	cd $(API_DIR) && .venv/bin/ruff check . && .venv/bin/ruff format --check .
	cd $(WEB_DIR) && npm run lint

typecheck:
	cd $(API_DIR) && .venv/bin/mypy
	cd $(WEB_DIR) && npm run typecheck

web-check:
	cd $(WEB_DIR) && npm run check

check: lint typecheck test web-check

openapi:
	cd $(API_DIR) && .venv/bin/python -m scripts.export_openapi

up:
	$(COMPOSE) up --build

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f

compose-config:
	$(COMPOSE) config
