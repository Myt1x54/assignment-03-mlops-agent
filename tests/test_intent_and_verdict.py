"""D37/D38: intent guard for namespace/deployment/DAG/replicas and the retraining-verdict coverage check."""

from __future__ import annotations

import pytest

from agent.llm import ToolCall
from agent.orchestrator import intent_mismatch, unverified_retraining_criteria
from agent.schemas import ToolError, ToolResult, ToolStatus
from agent.tools import load_all

load_all()
NS = "mlops-demo"


@pytest.mark.parametrize(
    ("question", "call"),
    [
        ("restart churn-api", ToolCall("k8s_restart_deployment", {})),
        ("restart the churn-api deployment", ToolCall("k8s_restart_deployment", {"deployment": "churn-api"})),
        ("restart the deployment please", ToolCall("k8s_restart_deployment", {})),
        ("scale churn-api to 3 replicas", ToolCall("k8s_scale_deployment", {"replicas": 3})),
        ("run the training pipeline", ToolCall("airflow_trigger_dag", {})),
        (
            "run the pipeline ml_training_pipeline",
            ToolCall("airflow_trigger_dag", {"dag_id": "ml_training_pipeline"}),
        ),
        (
            "validate data/incoming/churn_batch.csv with the training pipeline",
            ToolCall("airflow_trigger_dag", {"data_path": "data/incoming/churn_batch.csv"}),
        ),
        ("list pods in namespace prod", ToolCall("k8s_list_pods", {"namespace": "mlops-demo"})),  # read-only
    ],
)
def test_matching_or_unnamed_targets_are_allowed(question, call):
    assert intent_mismatch(question, call, NS) is None


REFUSED = [
    ("restart churn-api in namespace prod", "k8s_restart_deployment", {}, "namespace"),
    (
        "restart the billing-api deployment",
        "k8s_restart_deployment",
        {"deployment": "churn-api"},
        "deployment",
    ),
    ("restart deploy/billing-api", "k8s_restart_deployment", {}, "deployment"),
    ("scale churn-api to 10 replicas", "k8s_scale_deployment", {"replicas": 5}, "replica count"),
    ("trigger the cleanup_dag DAG", "airflow_trigger_dag", {"dag_id": "ml_training_pipeline"}, "DAG"),
    ("trigger cleanup_dag", "airflow_trigger_dag", {}, "DAG"),
]


@pytest.mark.parametrize(("question", "tool", "args", "label"), REFUSED)
def test_substituted_targets_are_refused(question, tool, args, label):
    message = intent_mismatch(question, ToolCall(tool, args), NS)
    assert message is not None
    assert label in message


def ok(tool: str) -> ToolResult:
    return ToolResult(tool=tool, request_id="r", status=ToolStatus.OK)


def down(tool: str) -> ToolResult:
    error = ToolError(type="ServiceUnavailableError", message="down")
    return ToolResult(tool=tool, request_id="r", status=ToolStatus.UNAVAILABLE, error=error)


def test_verdict_with_all_criteria_checked_passes():
    results = [ok("mlflow_best_run"), ok("airflow_list_dag_runs"), ok("dvc_list_dataset_versions")]
    assert unverified_retraining_criteria("Retraining is not indicated by the evidence.", results) == []


def test_verdict_with_mlflow_down_is_flagged():
    """F3 regression: MLflow unavailable but the model still said 'retraining is not indicated'."""
    results = [down("mlflow_best_run"), ok("airflow_list_dag_runs"), ok("dvc_list_dataset_versions")]
    missing = unverified_retraining_criteria("Retraining is not indicated by available evidence.", results)
    assert missing == ["model age and quality-gate status (MLflow)"]


def test_no_verdict_no_check():
    assert unverified_retraining_criteria("The deployment is healthy.", [down("mlflow_best_run")]) == []


@pytest.mark.parametrize(
    "text",
    [
        "Retraining is necessary.",
        "Retraining is NOT required",
        "There is no need to retrain.",
        "retraining recommended",
        "Retraining is not definitively indicated at this moment",
        "retraining is currently not needed",
        "Retraining is **not** currently indicated by the available evidence",
    ],
)
def test_verdict_phrasings_are_detected(text):
    assert unverified_retraining_criteria(text, []) != []
