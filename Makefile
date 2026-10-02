.DEFAULT_GOAL := help
.PHONY: help \
		install hooks \
		format lint test check \
		bronze silver gold quality pipeline \
		train evaluate \
		serve docker-build \
		clean

PY  := uv run python
PKG := bianque

help: ## Show available commands
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# --- Setup -------------------------------------------------------------------

install: ## Install exact versions from uv.lock
	uv sync --frozen

hooks: ## Install pre-commit hooks (gitleaks + ruff)
	uv run pre-commit install

# --- Code quality ------------------------------------------------------------

format: ## Auto-format and fix code
	uv run ruff format .
	uv run ruff check . --fix

lint: ## Check code without modifying it
	uv run ruff format --check .
	uv run ruff check .

test: ## Run test suite
	uv run pytest

check: lint test ## Lint + tests: run before every commit

# --- Data pipeline -----------------------------------------------------------

bronze: ## S3 CSV -> data/bronze Parquet (incremental by ETag)
	$(PY) -m $(PKG).pipeline.bronze

silver: ## Run sql/silver, apply contracts, quarantine bad rows
	$(PY) -m $(PKG).pipeline.silver

gold: ## Run sql/gold
	$(PY) -m $(PKG).pipeline.gold

quality: ## Run data quality checks and write reports/data_quality.md
	$(PY) -m $(PKG).quality.checks
	$(PY) -m $(PKG).quality.report

pipeline: bronze silver gold quality ## Full data pipeline

# --- Models and evaluation ---------------------------------------------------

train: ## Train model and log to MLflow
	$(PY) -m $(PKG).models.train

evaluate: ## Compare policies and run agent evaluation
	$(PY) eval/policies_compare.py
	$(PY) eval/run_agent_eval.py

# --- Serving -----------------------------------------------------------------

serve: ## Run FastAPI locally with reload
	uv run uvicorn $(PKG).api.main:app --reload

docker-build: ## Build Docker image
	docker build -t $(PKG):latest .

# --- Housekeeping ------------------------------------------------------------

clean: ## Remove caches (does not touch data/ or mlruns/)
	rm -rf .ruff_cache .pytest_cache
	find . -type d -name __pycache__ -exec rm -rf {} +
