# =============================================================================
# Atcraft Manufacturing ERP - developer entry points (master rules section 49A)
#
# On Windows without GNU make, use the identical PowerShell wrapper:
#     .\make.ps1 <target>          e.g.  .\make.ps1 dev
# =============================================================================
.DEFAULT_GOAL := help
.PHONY: help dev dev-api dev-web dev-worker dev-docker up down restart logs ps \
        migrate migrate-down migration seed seed-local demo-data db-fresh db-handover \
        deployment-guard deploy-init deploy-doctor deploy-up deploy-down \
        deploy-migrate deploy-health deploy-backup deploy-verify \
        test test-unit test-integration test-cov test-web test-e2e test-load build-web lint format \
        typecheck contract contract-check postman check lock hooks hooks-run \
        shell-api shell-db backup clean install

# The local development stack is a THIRD stack, separate from the two
# deployments, and it needs its own project name: docker-compose.yml requires
# COMPOSE_PROJECT_NAME so that a bare `docker compose up` cannot put two
# deployments into one project and have them share a database.
#
# `atcraft-erp` is deliberately the historical name, so the volumes an existing
# checkout already has - and the demo data in them - stay where they are.
COMPOSE_PROJECT_NAME ?= atcraft-erp
export COMPOSE_PROJECT_NAME

DC_DEV  := docker compose -p $(COMPOSE_PROJECT_NAME) -f docker-compose.yml -f docker-compose.dev.yml
API_DIR := apps/api
WEB_DIR := apps/web

# The two deployments are never driven from here. scripts/deploy.sh reads
# deploy/<name>.env, takes the project name from it and passes it explicitly on
# every command; a make variable would be one more place for the two to be
# confused. See ONE-BOX-RUNBOOK.md.
DEPLOY := ./scripts/deploy.sh

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install: ## Install backend + frontend dependencies for local (hybrid) development
	cd $(API_DIR) && python -m venv .venv && .venv/bin/python -m scripts.install_requirements requirements-dev.lock
	cd $(WEB_DIR) && npm ci
	@$(MAKE) hooks

hooks: ## Install the git pre-commit and pre-push hooks (.pre-commit-config.yaml)
	pre-commit install --hook-type pre-commit --hook-type pre-push

hooks-run: ## Run every hook against the whole tree, as a push would
	pre-commit run --all-files
	pre-commit run --all-files --hook-stage pre-push

lock: ## Regenerate reviewed Python dependency locks after editing pyproject.toml
	cd $(API_DIR) && .venv/bin/pip-compile --quiet --no-header --allow-unsafe --generate-hashes --strip-extras --resolver=backtracking --output-file=requirements.lock pyproject.toml
	cd $(API_DIR) && .venv/bin/pip-compile --quiet --no-header --allow-unsafe --extra=dev --generate-hashes --strip-extras --resolver=backtracking --output-file=requirements-dev.lock pyproject.toml

dev: ## Hybrid mode: Postgres+Redis in Docker, API and web run locally with hot reload
	$(DC_DEV) up -d postgres redis
	@echo "Infrastructure ready. Starting API and web locally..."
	@$(MAKE) -j2 dev-api dev-web

dev-api: ## Run FastAPI locally against Dockerised infrastructure
	cd $(API_DIR) && uvicorn src.main:app --reload --host 0.0.0.0 --port 8000

dev-web: ## Run the Vite dev server locally
	cd $(WEB_DIR) && npm run dev

dev-worker: ## Run a Celery worker locally against Dockerised Redis/Postgres
	cd $(API_DIR) && celery -A src.workers.celery_app:celery_app worker -Q default,mrp,reports --loglevel=INFO --pool=solo

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

seed-local: ## Migrate and populate a local database with all demonstration workflows
	cd $(API_DIR) && python -m scripts.seed_local

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

build-web: ## Production frontend build, bundle budgets and secret scan
	cd $(WEB_DIR) && npm run build

test-e2e: ## Playwright end-to-end tests against a running stack
	cd tests/e2e && npx playwright test

test-load: ## Locust load test (headless smoke profile)
	cd tests/load && locust -f locustfile.py --headless -u 50 -r 5 -t 2m --host http://localhost

# The same paths the pre-commit hook and CI lint: application code, tests,
# migrations, the backend scripts and the repository's hook wrappers.
PY_LINT_PATHS := src tests migrations scripts ../../scripts/hooks

lint: ## Lint and type-check backend + frontend
	cd $(API_DIR) && ruff check $(PY_LINT_PATHS) && ruff format --check $(PY_LINT_PATHS) && mypy src
	cd $(WEB_DIR) && npm run lint && npm run typecheck && npm run format:check

format: ## Format supported source files
	cd $(API_DIR) && ruff format $(PY_LINT_PATHS) && ruff check --fix $(PY_LINT_PATHS)
	cd $(WEB_DIR) && npm run format

typecheck: ## Type-check only
	cd $(API_DIR) && mypy src
	cd $(WEB_DIR) && npm run typecheck

contract: ## Regenerate the OpenAPI contract, the frontend types and the Postman collection
	cd $(API_DIR) && python -m scripts.export_openapi
	cd $(WEB_DIR) && npm run contract:types
	cd $(API_DIR) && python -m scripts.export_postman

contract-check: ## Fail if the contract, the generated types or the collection are stale
	cd $(API_DIR) && python -m scripts.export_openapi --check
	cd $(WEB_DIR) && npm run contract:check
	cd $(API_DIR) && python -m scripts.export_postman --check

postman: ## Regenerate the Postman collection from the committed OpenAPI contract
	cd $(API_DIR) && python -m scripts.export_postman

check: contract-check lint test-cov test-web build-web ## Application contract, quality, coverage, tests and production build

shell-api: ## Shell inside the backend container
	$(DC_DEV) exec backend bash

shell-db: ## psql inside the Postgres container
	$(DC_DEV) exec postgres psql -U $${POSTGRES_USER:-atcraft} -d $${POSTGRES_DB:-atcraft_erp}

backup: ## Dump the database to ./backups (never the only copy - section 45)
	@mkdir -p backups
	$(DC_DEV) exec -T postgres pg_dump -U $${POSTGRES_USER:-atcraft} -Fc $${POSTGRES_DB:-atcraft_erp} \
	  > backups/atcraft-$$(date +%Y%m%d-%H%M%S).dump
	@echo "Reminder: copy this dump to external storage. A backup is not reliable until a restore is tested."

# --- Deployments (two independent stacks on one host) --------------------
# Every target below takes DEPLOY=main or DEPLOY=dev. There is no default:
# a defaulted deployment name is a command that acts on whichever stack the
# author happened to have in mind.

deployment-guard:
	@test -n "$(D)" || { \
	  echo 'error: name the deployment, e.g.  make deploy-up D=main'; \
	  exit 1; }

deploy-init: deployment-guard ## Create deploy/<D>.env with generated secrets
	$(DEPLOY) $(D) init

deploy-doctor: deployment-guard ## Check deployment D's configuration
	$(DEPLOY) $(D) doctor

deploy-up: deployment-guard ## Guard, build, backup, migrate, and start deployment D
	$(DEPLOY) $(D) up

deploy-down: deployment-guard ## Stop deployment D (data kept)
	$(DEPLOY) $(D) down

deploy-migrate: deployment-guard ## Recovery-only standalone migration to head
	$(DEPLOY) $(D) migrate

deploy-health: deployment-guard ## Health of deployment D
	$(DEPLOY) $(D) health

deploy-backup: deployment-guard ## Dump deployment D's database
	$(DEPLOY) $(D) backup

deploy-verify: ## Prove the two deployments cannot reach each other
	./scripts/verify-isolation.sh

clean: ## Remove build artefacts and caches (keeps volumes)
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf $(API_DIR)/.pytest_cache $(API_DIR)/.ruff_cache $(API_DIR)/.mypy_cache $(API_DIR)/htmlcov
	rm -rf $(WEB_DIR)/dist $(WEB_DIR)/.vite
