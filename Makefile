.DEFAULT_GOAL := help
.PHONY: help \
		install hooks \
		format lint test check \
		bronze-plan bronze silver gold quality pipeline \
		label-signal train model-search evaluate \
		serve docker-build deploy \
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

bronze-plan: ## List pending S3 files per dataset without downloading
	$(PY) -m $(PKG).pipeline.bronze --dry-run

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

label-signal: ## Check for learnable fraud signal before training (reports/label_signal.md)
	$(PY) -m $(PKG).evaluation.label_signal

train: ## Fit the fraud calibrator, compare with baselines on frozen sets, log to MLflow
	uv run --group ml python -m $(PKG).models.train

model-search: ## Tuned ML vs the calibrated fraud_score (reports/model_search.md)
	uv run --group ml python -m $(PKG).evaluation.model_search

evaluate: ## Compare contact policies (reports/policy_comparison.md) and run agent evaluation
	$(PY) -m $(PKG).evaluation.policy_compare
	$(PY) eval/run_agent_eval.py

# --- Serving -----------------------------------------------------------------

serve: ## Run FastAPI locally with reload
	uv run uvicorn $(PKG).api.main:app --reload

docker-build: ## Build Docker image
	docker build -t $(PKG):latest .

deploy: ## Deploy the API to Google Cloud Run (GCP_* in .env; needs make gold)
	./scripts/deploy.sh

# --- Housekeeping ------------------------------------------------------------

clean: ## Remove caches (does not touch data/ or mlruns/)
	rm -rf .ruff_cache .pytest_cache
	find . -type d -name __pycache__ -exec rm -rf {} +
