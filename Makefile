# Convenience wrappers. Every target is a thin alias for a plain `docker compose`
# command - see README.md for the raw equivalents (needed on Windows, which has no make).
.DEFAULT_GOAL := help
DC := docker compose
EXEC := $(DC) exec -T api

.PHONY: help up down logs migrate revision seed test test-cov lint fmt openapi reset demo-events chaos-on chaos-off

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n",$$1,$$2}'

up: ## Build and start the full stack
	$(DC) up -d --build

down: ## Stop the stack (keeps the database volume)
	$(DC) down

reset: ## Stop the stack AND delete all data
	$(DC) down -v

logs: ## Tail API logs
	$(DC) logs -f api

migrate: ## Apply database migrations
	$(EXEC) alembic upgrade head

revision: ## Autogenerate a migration: make revision m="add table"
	$(EXEC) alembic revision --autogenerate -m "$(m)"

seed: ## Load mock users, resources and sample disasters
	$(EXEC) python -m scripts.seed

test: ## Run the test suite
	$(EXEC) pytest

test-cov: ## Run tests with a coverage report
	$(EXEC) pytest --cov=app --cov-report=term-missing

lint: ## Lint
	$(EXEC) ruff check app tests mock_services scripts

fmt: ## Auto-format and auto-fix
	$(EXEC) ruff format app tests mock_services scripts && $(EXEC) ruff check --fix app tests mock_services scripts

openapi: ## Export docs/openapi.json from the live app
	$(EXEC) python -m scripts.export_openapi

demo-events: ## Print real-time events to the terminal (SSE client)
	$(EXEC) python -m scripts.demo_events

chaos-on: ## Make the mock external provider fail 80% of requests
	$(DC) exec -T mock-services curl -fsS -X POST "http://localhost:9000/_control/chaos?failure_rate=0.8"

chaos-off: ## Restore the mock external provider
	$(DC) exec -T mock-services curl -fsS -X POST "http://localhost:9000/_control/chaos?failure_rate=0.0"
