"""Agent-level tests (PLAN S14 required tests 1-9): scripted LLM + mocked backends, no network."""

from __future__ import annotations

import io
import json
import logging
import time
from types import SimpleNamespace as NS

import pytest

import agent.tools.airflow_tools as af
import agent.tools.dvc_tools as dvc
import agent.tools.kubernetes_tools as kt
import agent.tools.mlflow_tools as mt
from agent.config import Settings, load_policy
from agent.errors import ServiceUnavailableError
from agent.llm import LLMReply, ScriptedLLM, ToolCall
from agent.logging_utils import JsonFormatter
from agent.orchestrator import Orchestrator, ungrounded_figures
from agent.schemas import ToolResult, ToolStatus
from tests.test_airflow_tools import FakeAirflow
from tests.test_dvc_tools import V2_MD5, FakeGitDvc
from tests.test_kubernetes_tools import deployment, install, pod
from tests.test_mlflow_tools import FakeClient

SECRET = "airflow-secret-pw-999"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(_env_file=None, airflow_password=SECRET, agent_tool_timeout_seconds=2)


@pytest.fixture
def backends(monkeypatch):
    """All four systems healthy (fakes)."""
    monkeypatch.setattr(mt, "get_client", lambda c: FakeClient())
    monkeypatch.setattr(dvc.subprocess, "run", FakeGitDvc())
    fake_af = FakeAirflow()
    monkeypatch.setattr(af.requests, "post", fake_af.post)
    monkeypatch.setattr(af.requests, "request", fake_af.request)
    af._tokens.clear()
    apis = install(monkeypatch, [pod("a", True), pod("b", True)], deployment(2, 2), 2)
    return NS(airflow=fake_af, k8s=apis)


def agent_with(steps, settings, confirm=None) -> tuple[Orchestrator, ScriptedLLM]:
    llm = ScriptedLLM(steps)
    kwargs = {"confirm": confirm} if confirm else {}
    return Orchestrator(llm, settings=settings, policy=load_policy(), **kwargs), llm


def last_tool_payloads(llm: ScriptedLLM) -> list[dict]:
    history = llm.calls[-1]["history"]
    tool_msgs = [m for m in history if m["role"] == "tool"]
    return [payload for _, payload in tool_msgs[-1]["results"]]


# 1 ---------------------------------------------------------------------------------------------------------
def test_tool_selection_result_flows_back_to_llm(settings, backends):
    def answer(history):
        payload = history[-1]["results"][0][1]
        best = payload["data"]["best"]
        return LLMReply(text=f"Best run {best['run_id'][:8]} with F1 {best['metrics']['f1']} [E1].")

    agent, llm = agent_with([LLMReply(tool_calls=[ToolCall("mlflow_best_run", {})]), answer], settings)
    turn = agent.ask("Which run has the best F1?")
    assert [r.tool for r in turn.results] == ["mlflow_best_run"]
    assert turn.results[0].evidence_id == "E1"
    assert "F1 0.83 [E1]" in turn.response.answer
    assert "mlflow_best_run" in llm.calls[0]["tools"]  # schema was offered to the LLM
    assert "Grounding check" not in turn.response.answer


# 2 ---------------------------------------------------------------------------------------------------------
def test_multi_tool_request_spans_all_four_systems(settings, backends):
    calls = [
        ToolCall("mlflow_best_run", {"only_passed_gate": True}),
        ToolCall("dvc_get_run_dataset", {"run_id": "a" * 32}),
        ToolCall("k8s_find_unhealthy", {}),
        ToolCall("airflow_list_dag_runs", {}),
    ]
    agent, _ = agent_with(
        [LLMReply(tool_calls=calls), LLMReply(text="Correlated answer [E1][E2][E3][E4].")], settings
    )
    turn = agent.ask("Find the best recent model, its dataset, deployment status and whether to retrain.")
    assert [r.status for r in turn.results] == [ToolStatus.OK] * 4
    systems = {r.tool.split("_")[0] for r in turn.results}
    assert systems == {"mlflow", "dvc", "k8s", "airflow"}
    assert [r.evidence_id for r in turn.results] == ["E1", "E2", "E3", "E4"]
    assert "| E4 | `airflow_list_dag_runs` | ok |" in turn.response.answer
    assert not any(m == "POST" for m, _, _ in backends.airflow.calls)  # analysis never triggers a DAG


# 3 ---------------------------------------------------------------------------------------------------------
def test_tool_timeout_is_reported(settings, backends, monkeypatch):
    class Slow(FakeClient):
        def search_experiments(self):
            time.sleep(5)
            return super().search_experiments()

    monkeypatch.setattr(mt, "get_client", lambda c: Slow())
    agent, llm = agent_with(
        [LLMReply(tool_calls=[ToolCall("mlflow_list_experiments", {})]), LLMReply(text="MLflow timed out.")],
        settings,
    )
    turn = agent.ask("List experiments")
    assert turn.results[0].status is ToolStatus.TIMEOUT
    assert last_tool_payloads(llm)[0]["status"] == "timeout"
    assert "Not verified" in turn.response.answer


# 4 ---------------------------------------------------------------------------------------------------------
def test_invalid_tool_input_returns_validation_error_to_llm(settings, backends):
    bad = ToolCall("k8s_scale_deployment", {"replicas": "lots", "confirmed": True})
    agent, llm = agent_with(
        [LLMReply(tool_calls=[bad]), LLMReply(text="Could not scale: invalid input.")], settings
    )
    turn = agent.ask("scale it")
    payload = last_tool_payloads(llm)[0]
    assert payload["status"] == "error"
    assert payload["error"]["type"] == "ValidationError"
    assert turn.response.stopped_reason == "completed"
    assert backends.k8s.apps.patches == []


# 5 ---------------------------------------------------------------------------------------------------------
def test_mlflow_down_is_unavailable(settings, backends, monkeypatch):
    class Down:
        def search_experiments(self):
            raise Exception("Max retries exceeded with url: /api/2.0/mlflow/experiments/search")

    monkeypatch.setattr(mt, "get_client", lambda c: Down())
    agent, _ = agent_with(
        [LLMReply(tool_calls=[ToolCall("mlflow_best_run", {})]), LLMReply(text="MLflow is unavailable.")],
        settings,
    )
    turn = agent.ask("best model?")
    assert turn.results[0].status is ToolStatus.UNAVAILABLE
    assert "| E1 | `mlflow_best_run` | unavailable |" in turn.response.answer


# 6 ---------------------------------------------------------------------------------------------------------
def test_no_invention_when_kubernetes_unavailable(settings, backends, monkeypatch):
    def no_cluster(ctx):
        raise ServiceUnavailableError("Kubernetes API unreachable")

    monkeypatch.setattr(kt, "get_apis", no_cluster)
    honest = LLMReply(text="Deployment status could not be retrieved: Kubernetes is unavailable [E1].")
    agent, _ = agent_with([LLMReply(tool_calls=[ToolCall("k8s_list_deployments", {})]), honest], settings)
    turn = agent.ask("Is the inference service healthy?")
    answer = turn.response.answer
    assert turn.results[0].status is ToolStatus.UNAVAILABLE
    assert "| E1 | `k8s_list_deployments` | unavailable |" in answer
    assert "Facts from kubernetes could not be confirmed" in answer
    for invented in ("2/2", "replicas ready", "Running"):
        assert invented not in answer


def test_grounding_check_flags_invented_figures(settings, backends, monkeypatch):
    def no_cluster(ctx):
        raise ServiceUnavailableError("Kubernetes API unreachable")

    monkeypatch.setattr(kt, "get_apis", no_cluster)
    lying = LLMReply(text="All good: 3 replicas are ready and model v9 is serving with F1 0.97.")
    agent, _ = agent_with([LLMReply(tool_calls=[ToolCall("k8s_list_deployments", {})]), lying], settings)
    answer = agent.ask("Is it healthy?").response.answer
    assert "Grounding check" in answer
    for fig in ("`3`", "`v9`", "`0.97`"):
        assert fig in answer


def test_ungrounded_figures_accepts_values_present_in_results():
    result = ToolResult(tool="t", request_id="r", status=ToolStatus.OK, data={"f1": 0.8121, "version": "7"})
    assert ungrounded_figures("F1 is 0.8121 for v7 [E1]", [result]) == []
    assert ungrounded_figures("F1 is 0.95", [result]) == ["0.95"]


# 7 ---------------------------------------------------------------------------------------------------------
def test_unauthorized_k8s_change_wrong_namespace_rejected(settings, backends):
    confirmations = []
    call = ToolCall("k8s_restart_deployment", {"namespace": "default"})
    agent, _ = agent_with(
        [LLMReply(tool_calls=[call]), LLMReply(text="Rejected.")],
        settings,
        confirm=lambda s, a: confirmations.append(s) or True,
    )
    turn = agent.ask("restart churn-api in default")
    assert turn.results[0].status is ToolStatus.REJECTED
    assert confirmations == []  # never even asked: policy rejects first
    assert backends.k8s.apps.patches == []


def test_k8s_change_declined_by_human_is_not_executed(settings, backends):
    agent, llm = agent_with(
        [LLMReply(tool_calls=[ToolCall("k8s_restart_deployment", {})]), LLMReply(text="Not restarted.")],
        settings,
        confirm=lambda s, a: False,
    )
    turn = agent.ask("restart churn-api")
    assert turn.results[0].status is ToolStatus.REJECTED
    assert turn.results[0].error.type == "DeclinedByUser"
    assert turn.confirmations == [
        {
            "tool": "k8s_restart_deployment",
            "summary": "Rollout restart of deployment 'churn-api' in namespace 'mlops-demo'",
            "approved": False,
        }
    ]
    assert backends.k8s.apps.patches == []
    assert "confirmation_token" not in json.dumps(last_tool_payloads(llm), default=str)


def test_k8s_change_approved_by_human_executes(settings, backends):
    seen = {}
    agent, _ = agent_with(
        [LLMReply(tool_calls=[ToolCall("k8s_restart_deployment", {})]), LLMReply(text="Restarted [E1].")],
        settings,
        confirm=lambda s, a: seen.update(action=a) or True,
    )
    turn = agent.ask("please restart churn-api")
    assert turn.results[0].status is ToolStatus.OK
    assert "confirmation_token" not in seen["action"]  # the human sees the action, not the token
    assert backends.k8s.apps.patches[0][:3] == ("restart", "churn-api", "mlops-demo")


# 8 ---------------------------------------------------------------------------------------------------------
def test_non_approved_dag_trigger_rejected(settings, backends):
    agent, _ = agent_with(
        [
            LLMReply(tool_calls=[ToolCall("airflow_trigger_dag", {"dag_id": "cleanup_dag"})]),
            LLMReply(text="No."),
        ],
        settings,
        confirm=lambda s, a: True,
    )
    turn = agent.ask("trigger cleanup_dag")
    assert turn.results[0].status is ToolStatus.REJECTED
    assert not any(m == "POST" for m, _, _ in backends.airflow.calls)


# 9 ---------------------------------------------------------------------------------------------------------
def test_structured_logs_have_fields_and_no_secrets(settings, backends):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger("agent")
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        agent, _ = agent_with(
            [LLMReply(tool_calls=[ToolCall("airflow_list_dags", {})]), LLMReply(text=f"done {SECRET}")],
            settings,
        )
        turn = agent.ask(f"list dags, my password is {SECRET}")
    finally:
        root.removeHandler(handler)
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    tool_line = next(entry for entry in lines if entry.get("tool") == "airflow_list_dags")
    assert tool_line["status"] == "ok"
    assert isinstance(tool_line["duration_ms"], int)
    assert tool_line["request_id"] == turn.response.request_id
    assert SECRET not in stream.getvalue()


def test_llm_failure_is_reported_not_invented(settings, backends):
    class Broken:
        def generate(self, system, history, tools):
            raise RuntimeError("quota exhausted")

    agent = Orchestrator(Broken(), settings=settings, policy=load_policy())
    turn = agent.ask("anything")
    assert turn.response.stopped_reason == "llm_error"
    assert "could not be reached" in turn.response.answer


def test_iteration_budget_stops_tool_loops(settings, backends):
    looping = [LLMReply(tool_calls=[ToolCall("dvc_detect_changes", {})]) for _ in range(30)]
    agent, _ = agent_with(looping, settings.model_copy(update={"agent_max_iterations": 3}))
    turn = agent.ask("loop forever")
    assert turn.response.stopped_reason == "max_iterations"
    assert turn.response.iterations == 3


def test_dataset_provenance_correlation(settings, backends, monkeypatch):
    monkeypatch.setattr(
        mt, "get_run_tags", lambda run_id, c: {"data.version": "data-v2.0", "data.dvc_md5": V2_MD5}
    )
    agent, _ = agent_with(
        [
            LLMReply(tool_calls=[ToolCall("dvc_get_run_dataset", {"run_id": "a" * 32})]),
            LLMReply(text="data-v2.0 [E1]"),
        ],
        settings,
    )
    turn = agent.ask("which data trained run aaaa?")
    assert turn.results[0].data["verified_against_git"] is True
