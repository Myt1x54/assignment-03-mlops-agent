"""Static checks of the Airflow DAG file (no Airflow install needed, so this runs in CI)."""

from __future__ import annotations

import ast
from pathlib import Path

DAG_FILE = Path(__file__).resolve().parents[1] / "dags" / "training_pipeline.py"
TREE = ast.parse(DAG_FILE.read_text(encoding="utf-8"))
SOURCE = DAG_FILE.read_text(encoding="utf-8")


def _operators() -> dict[str, dict[str, ast.expr]]:
    tasks: dict[str, dict[str, ast.expr]] = {}
    for node in ast.walk(TREE):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "BashOperator":
            kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
            tasks[ast.literal_eval(kwargs["task_id"])] = kwargs
    return tasks


def test_pipeline_has_all_required_stages():
    assert list(_operators()) == [
        "validate_data",
        "preprocess",
        "train",
        "evaluate_and_log",
        "quality_gate",
        "promote_model",
    ]


def test_dependency_chain_is_linear_and_gate_precedes_promotion():
    chain = "validate_data >> preprocess >> train >> evaluate_and_log >> quality_gate >> promote_model"
    assert chain in SOURCE


def test_retries_configured_and_deterministic_checks_fail_fast():
    assert '"retries": 2' in SOURCE
    assert "retry_exponential_backoff" in SOURCE
    ops = _operators()
    for task in ("validate_data", "quality_gate"):
        assert ast.literal_eval(ops[task]["retries"]) == 0


def test_dag_id_and_manual_schedule():
    assert 'dag_id="ml_training_pipeline"' in SOURCE
    assert "schedule=None" in SOURCE
    assert "max_active_runs=1" in SOURCE
