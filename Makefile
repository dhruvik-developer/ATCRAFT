# =============================================================================
# Atcraft Manufacturing ERP - developer entry points (master rules section 49A)
#
# On Windows without GNU make, use the identical PowerShell wrapper:
#     .\make.ps1 <target>          e.g.  .\make.ps1 dev
# =============================================================================
.DEFAULT_GOAL := help
.PHONY: help dev dev-docker up down restart logs ps migrate migration seed demo-data db-fresh db-handover \
        test test-unit test-integration test-e2e test-load lint format \
        typecheck check shell-api shell-db backup restore clean install

DC_DEV  := docker compose -f docker-compose.yml -f docker-compose.dev.yml
DC_PROD := docker compose -f docker-compose.yml -f docker-compose.prod.yml
API_DIR := apps/api
WEB_DIR := apps/web

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install: ## Install backend + frontend dependencies for local (hybrid) development
	cd $(API_DIR) && python -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
	cd $(WEB_DIR) && npm ci

dev: ## Hybrid mode: Postgres+Redis in Docker, API and web run locally with hot reload
	$(DC_DEV) up -d postgres redis
	@echo "Infrastructure ready. Starting API and web locally..."
	@$(MAKE) -j2 dev-api dev-web

dev-api: ## Run FastAPI locally against Dockerised infrastructure
	cd $(API_DIR) && uvicorn src.main:app --reload --host 0.0.0.0 --port 8000

dev-web: ## Run the Vite dev server locally
	cd $(WEB_DIR) && npm run dev

dev-worker: ## Run a Celery worker locally against Dockerised Redis/Postgres
	cd $(API_DIR) && celery -A src.workers.celery_app:celery_app worker --loglevel=INFO --pool=solo

dev-docker: ## Full Docker development environment (nginx, web, api, pg, redis, worker)
	$(DC_DEV) up --build

up: ## Start the standard Docker Compose stack in the background
	$(DC_DEV) up -d

down: ## Stop the stack (volumes preserved)
	$(DC_DEV) down

restart: down up ## Restart the stack

logs: ## Tail useful service logs
	$(DC_DEV) logs -f --tail=100 backend celery-worker frontend nginx

ps: ## Show service status
	$(DC_DEV) ps

migrate: ## Apply Alembic migrations to head
	cd $(API_DIR) && alembic upgrade head

migrate-down: ## Roll back one Alembic revision
	cd $(API_DIR) && alembic downgrade -1

migration: ## Autogenerate a migration:  make migration m="add xyz table"
	cd $(API_DIR) && alembic revision --autogenerate -m "$(m)"

seed: ## Load baseline reference data (roles, permissions, UoM, warehouse types)
	cd $(API_DIR) && python -m src.core.seed

demo-data: ## Fill a development database with a working business (run after `seed`)
	cd $(API_DIR) && python -m src.core.demo_data

db-fresh: ## DESTROYS the dev database, then rebuilds it with baseline + demo data
	cd $(API_DIR) && python -m scripts.reset_database --yes --demo

db-handover: ## DESTROYS the dev database, then rebuilds it with the baseline ONLY
	cd $(API_DIR) && python -m scripts.reset_database --yes --seed

test: test-unit test-integration ## Run backend unit + integration tests

test-unit: ## Fast unit tests (no database required)
	cd $(API_DIR) && pytest tests/unit -q

test-integration: ## Integration + API tests (requires Postgres and Redis)
	cd $(API_DIR) && pytest tests/integration tests/api -q

test-cov: ## Backend tests with coverage report
	cd $(API_DIR) && pytest --cov=src --cov-report=term-missing --cov-report=html

test-web: ## Frontend unit tests
	cd $(WEB_DIR) && npm run test

test-e2e: ## Playwright end-to-end tests against a running stack
	cd tests/e2e && npx playwright test

test-load: ## Locust load test (headless smoke profile)
	cd tests/load && locust -f locustfile.py --headless -u 50 -r 5 -t 2m --host http://localhost

lint: ## Lint and type-check backend + frontend
	cd $(API_DIR) && ruff check src tests && mypy src
	cd $(WEB_DIR) && npm run lint && npm run typecheck

format: ## Format supported source files
	cd $(API_DIR) && ruff format src tests && ruff check --fix src tests
	cd $(WEB_DIR) && npm run format

typecheck: ## Type-check only
	cd $(API_DIR) && mypy src
	cd $(WEB_DIR) && npm run typecheck

contract: ## Regenerate the OpenAPI contract and the frontend types from it
	cd $(API_DIR) && python scripts/export_openapi.py
	cd $(WEB_DIR) && npm run contract:types

contract-check: ## Fail if the contract or the generated types are stale
	cd $(API_DIR) && python scripts/export_openapi.py --check
	cd $(WEB_DIR) && npm run contract:check

check: contract-check lint test ## Everything CI runs

shell-api: ## Shell inside the backend container
	$(DC_DEV) exec backend bash

shell-db: ## psql inside the Postgres container
	$(DC_DEV) exec postgres psql -U $${POSTGRES_USER:-atcraft} -d $${POSTGRES_DB:-atcraft_erp}

backup: ## Dump the database to ./backups (never the only copy - section 45)
	@mkdir -p backups
	$(DC_DEV) exec -T postgres pg_dump -U $${POSTGRES_USER:-atcraft} -Fc $${POSTGRES_DB:-atcraft_erp} \
	  > backups/atcraft-$$(date +%Y%m%d-%H%M%S).dump
	@echo "Reminder: copy this dump to external storage. A backup is not reliable until a restore is tested."

prod-up: ## Start the production stack
	$(DC_PROD) up -d --build

prod-down: ## Stop the production stack
	$(DC_PROD) down

clean: ## Remove build artefacts and caches (keeps volumes)
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf $(API_DIR)/.pytest_cache $(API_DIR)/.ruff_cache $(API_DIR)/.mypy_cache $(API_DIR)/htmlcov
	rm -rf $(WEB_DIR)/dist $(WEB_DIR)/.vite
