"""Live integration checks against the local stack (MLflow, Airflow, kind). Skipped by default and in CI.

uv run pytest -m integration -q
"""

from __future__ import annotations

import pytest

from agent.schemas import ToolStatus
from agent.tools import load_all
from agent.tools.base import ToolContext, execute_tool

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def ctx() -> ToolContext:
    load_all()
    return ToolContext(request_id="req-integration")


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("dvc_get_current_version", {}),
        ("dvc_compare_versions", {"rev_a": "data-v1.0", "rev_b": "data-v2.0"}),
        ("mlflow_best_run", {"only_passed_gate": True}),
        ("mlflow_get_model_versions", {}),
        ("airflow_list_dags", {}),
        ("airflow_list_dag_runs", {}),
        ("k8s_find_unhealthy", {}),
        ("k8s_get_deployed_version", {}),
    ],
)
def test_live_read_tools_ok(ctx, tool, args):
    result = execute_tool(tool, args, ctx)
    assert result.status is ToolStatus.OK, result.error


def test_live_champion_is_what_is_deployed(ctx):
    registry = execute_tool("mlflow_get_model_versions", {}, ctx).data
    deployed = execute_tool("k8s_get_deployed_version", {}, ctx).data
    assert deployed["live_version"]["model_version"] == registry["aliases"]["champion"]


def test_live_write_guards(ctx):
    assert execute_tool("k8s_restart_deployment", {"namespace": "default"}, ctx).status is ToolStatus.REJECTED
    assert execute_tool("k8s_restart_deployment", {}, ctx).status is ToolStatus.CONFIRMATION_REQUIRED
    assert execute_tool("airflow_trigger_dag", {"dag_id": "other"}, ctx).status is ToolStatus.REJECTED
