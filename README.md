# factored-hackathon-2026-bianque
The best complaint is the one that never arrives. Proactive AI customer service for LATAM banking. It scores card charges for fraud, contacts customers only when expected loss outweighs channel cost, and resolves disputes in Spanish and Portuguese with verified actions and safe human handoff.

> **Status:** early scaffold. The `bianque` package (`src/bianque/`) and tooling are in place; pipeline, model and API modules are not implemented yet.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- Docker (optional, for `make docker-build`)

## Setup

```bash
git clone <repo-url> && cd factored-hackathon-2026-bianque
make install   # uv sync --frozen (exact versions from uv.lock)
make hooks     # install pre-commit hooks (gitleaks, ruff, hygiene checks)
```

Run `make` or `make help` to list all commands.

## Development

```bash
make format    # auto-format and fix with ruff
make lint      # check formatting and lint without modifying files
make test      # run pytest (tests/)
```

Pre-commit hooks run gitleaks (secret scanning), basic file checks and ruff on every commit. Never commit credentials; keep them in a local `.env` (ignored by git).

## Planned workflow

These targets are defined in the `Makefile` but their modules are still to be written:

| Step | Command | What it does |
|------|---------|--------------|
| Data | `make pipeline` | bronze (S3 CSV → Parquet) → silver (contracts, quarantine) → gold → quality report |
| Model | `make train` | train fraud model and log to MLflow |
| Eval | `make evaluate` | compare contact policies and run agent evaluation |
| Serve | `make serve` | run the FastAPI app locally |

Pipeline outputs (`data/`) and MLflow artifacts (`mlruns/`) are git-ignored.
