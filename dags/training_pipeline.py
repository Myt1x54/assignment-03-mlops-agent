"""Airflow 3 training pipeline: ``ml_training_pipeline`` (DECISION D9, D10).

    validate_data -> preprocess -> train -> evaluate_and_log -> quality_gate -> promote_model

* Each task runs the project's training module in an isolated virtualenv (``/opt/ml-venv``) so the ML stack
  never conflicts with Airflow's own dependencies.
* Transient-failure tasks (``preprocess``, ``train``, ``evaluate_and_log``, ``promote_model``) retry twice.
* Deterministic-failure tasks (``validate_data``, ``quality_gate``) do **not** retry: bad data or a weak model
  will not get better by retrying, and failing fast keeps diagnostics clear.
* Default trigger rule ``all_success``: a failed validation or quality gate leaves every downstream task in
  ``upstream_failed`` — a model can never be promoted past a failed check.
* Run-time parameter ``data_path`` (default ``data/raw/churn.csv`` = DVC-tracked dataset) lets an operator
  validate a newly arrived batch, e.g. ``{"data_path": "data/incoming/churn_batch.csv"}``.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import DAG, Param

PROJECT_DIR = "/opt/project"
PY = "/opt/ml-venv/bin/python"
DATA = "{{ params.data_path }}"
RUN_ID = "{{ run_id }}"


def step(module: str, args: str = "") -> str:
    """Shell command that runs ``python -m training.<module>`` inside the project directory."""
    return f"set -euo pipefail; cd {PROJECT_DIR} && {PY} -m training.{module} {args}"


default_args = {
    "owner": "mlops",
    "retries": 2,
    "retry_delay": timedelta(seconds=20),
    "retry_exponential_backoff": True,
    "execution_timeout": timedelta(minutes=10),
}

with DAG(
    dag_id="ml_training_pipeline",
    description="Validate data, train the churn model, log to MLflow, gate on quality, promote",
    start_date=datetime(2026, 11, 1),
    schedule=None,
    catchup=False,
    max_active_runs=1,
    default_args=default_args,
    tags=["mlops", "training", "churn"],
    params={
        "data_path": Param("data/raw/churn.csv", type="string", description="Dataset file inside the repo"),
    },
    doc_md=__doc__,
) as dag:
    validate_data = BashOperator(
        task_id="validate_data",
        bash_command=step("validate", f"--data {DATA} --report reports/validation.json"),
        retries=0,
    )

    preprocess = BashOperator(
        task_id="preprocess",
        bash_command=step("preprocess", f"--data {DATA} --out-dir data/processed"),
    )

    train = BashOperator(
        task_id="train",
        bash_command=step("train", "--train data/processed/train.csv --model-dir models"),
    )

    evaluate_and_log = BashOperator(
        task_id="evaluate_and_log",
        bash_command=step(
            "evaluate",
            "--test data/processed/test.csv --model-dir models --reports-dir reports "
            f"--pipeline-run-id '{RUN_ID}'",
        ),
    )

    quality_gate = BashOperator(
        task_id="quality_gate",
        bash_command=step("quality_gate", "--metrics reports/metrics.json --action check"),
        retries=0,
    )

    promote_model = BashOperator(
        task_id="promote_model",
        bash_command=step("quality_gate", "--metrics reports/metrics.json --action promote"),
    )

    validate_data >> preprocess >> train >> evaluate_and_log >> quality_gate >> promote_model
