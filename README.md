# Assignment 03 — Production-Ready MLOps AI Agent

Conversational agent that inspects and operates an ML system built on **DVC, MLflow, Apache Airflow 3 and
Kubernetes**, answering only from real tool evidence.

> Work in progress — see `PLAN.md` for the step-by-step plan and `DECISION.md` for design decisions.
> Full installation and usage instructions are written in step S18.

## Quick start (development)

```bash
uv sync                    # creates .venv with Python 3.12 and locked dependencies
uv run ruff check .        # lint
uv run pytest -q           # tests
```

Copy `.env.example` to `.env` and fill in values. Never commit `.env`.
