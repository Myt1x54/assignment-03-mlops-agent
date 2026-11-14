"""S11: Airflow tools with a mocked REST API (no Airflow needed)."""

from __future__ import annotations

from typing import Any

import pytest
import requests

import agent.tools.airflow_tools as af
from agent.config import Settings, load_policy
from agent.schemas import ToolStatus
from agent.tools.base import ToolContext, execute_tool

PASSWORD = "very-secret-pw-123"
RUN_FAILED = "manual__2026-11-14T10:00:00+00:00_bad"


class Resp:
    def __init__(self, status: int, body: Any = None) -> None:
        self.status_code = status
        self._body = body or {}
        self.text = str(body)

    def json(self) -> Any:
        return self._body


class FakeAirflow:
    """Routes the endpoints the tools use; records calls (method, path, json)."""

    def __init__(self, login_status: int = 200) -> None:
        self.login_status = login_status
        self.calls: list[tuple[str, str, Any]] = []

    def post(self, url, json=None, timeout=None):
        assert url.endswith("/auth/token")
        if self.login_status != 200:
            return Resp(self.login_status, {"detail": "Invalid credentials"})
        return Resp(200, {"access_token": "jwt-token-abcdefghijklmnop"})

    def request(self, method, url, headers=None, timeout=None, params=None, json=None):
        path = url.split("/api/v2", 1)[1]
        self.calls.append((method, path, json))
        if path == "/dags":
            return Resp(200, {"total_entries": 2, "dags": [
                {"dag_id": "ml_training_pipeline", "is_paused": False, "tags": [{"name": "mlops"}]},
                {"dag_id": "cleanup_dag", "is_paused": True, "tags": []},
            ]})  # fmt: skip
        if path == "/dags/ml_training_pipeline/dagRuns" and method == "GET":
            return Resp(
                200, {"total_entries": 1, "dag_runs": [{"dag_run_id": RUN_FAILED, "state": "failed"}]}
            )
        if path == "/dags/ml_training_pipeline/dagRuns" and method == "POST":
            return Resp(200, {"dag_run_id": "manual__new", "state": "queued", "conf": json["conf"]})
        if path == f"/dags/ml_training_pipeline/dagRuns/{RUN_FAILED}":
            return Resp(200, {"state": "failed", "conf": {"data_path": "data/incoming/churn_batch.csv"}})
        if path.endswith("/taskInstances"):
            return Resp(200, {"task_instances": [
                {"task_id": "validate_data", "state": "failed", "try_number": 1, "start_date": "2026-11-14"},
                {"task_id": "preprocess", "state": "upstream_failed", "try_number": 0, "start_date": None},
                {"task_id": "train", "state": "upstream_failed", "try_number": 0, "start_date": None},
            ]})  # fmt: skip
        if path.endswith("/taskInstances/validate_data"):
            return Resp(200, {"try_number": 1})
        if "/logs/" in path:
            return Resp(200, {"content": [
                {"event": "validation FAILED: rows=5000 errors=3", "level": "info"},
                {"event": f"connecting with password={PASSWORD}", "level": "info"},
                {"event": "Command exited with return code 1", "level": "error"},
            ]})  # fmt: skip
        return Resp(404)


@pytest.fixture
def fake(monkeypatch) -> FakeAirflow:
    f = FakeAirflow()
    monkeypatch.setattr(af.requests, "post", f.post)
    monkeypatch.setattr(af.requests, "request", f.request)
    af._tokens.clear()
    return f


@pytest.fixture
def ctx() -> ToolContext:
    settings = Settings(_env_file=None, airflow_password=PASSWORD)
    return ToolContext(request_id="req-af", settings=settings, policy=load_policy())


def test_list_dags_marks_approved(fake, ctx):
    r = execute_tool("airflow_list_dags", {}, ctx)
    flags = {d["dag_id"]: d["agent_may_trigger"] for d in r.data["dags"]}
    assert flags == {"ml_training_pipeline": True, "cleanup_dag": False}


def test_task_instances_identify_failure_and_downstream(fake, ctx):
    r = execute_tool("airflow_get_task_instances", {}, ctx)
    assert r.status is ToolStatus.OK
    assert r.data["dag_run_id"] == RUN_FAILED  # defaults to latest run
    assert r.data["first_failed_task"] == "validate_data"
    assert r.data["upstream_failed_tasks"] == ["preprocess", "train"]
    assert r.data["conf"]["data_path"] == "data/incoming/churn_batch.csv"


def test_task_logs_tail_redacts_secrets(fake, ctx):
    r = execute_tool("airflow_get_task_logs", {"task_id": "validate_data"}, ctx)
    text = str(r.data)
    assert "validation FAILED" in text
    assert PASSWORD not in text
    assert any("return code 1" in line for line in r.data["error_lines"])


def test_trigger_non_approved_dag_is_rejected_without_api_call(fake, ctx):
    r = execute_tool("airflow_trigger_dag", {"dag_id": "cleanup_dag"}, ctx)
    assert r.status is ToolStatus.REJECTED
    assert not any(m == "POST" for m, _, _ in fake.calls)


def test_trigger_requires_confirmation_then_runs(fake, ctx):
    args = {"data_path": "data/incoming/churn_batch.csv"}
    first = execute_tool("airflow_trigger_dag", args, ctx)
    assert first.status is ToolStatus.CONFIRMATION_REQUIRED
    assert not any(m == "POST" for m, _, _ in fake.calls)  # nothing triggered yet

    ctx.confirmation_token = first.data["action"]["confirmation_token"]
    second = execute_tool("airflow_trigger_dag", args, ctx)
    assert second.status is ToolStatus.OK
    assert second.data["triggered"] is True
    posted = [j for m, _, j in fake.calls if m == "POST"]
    assert posted == [{"logical_date": None, "conf": {"data_path": "data/incoming/churn_batch.csv"}}]


def test_auth_failure_is_error_and_leaks_nothing(monkeypatch, ctx):
    f = FakeAirflow(login_status=401)
    monkeypatch.setattr(af.requests, "post", f.post)
    monkeypatch.setattr(af.requests, "request", f.request)
    af._tokens.clear()
    r = execute_tool("airflow_list_dags", {}, ctx)
    assert r.status is ToolStatus.ERROR
    assert "authentication failed" in r.error.message
    assert PASSWORD not in r.model_dump_json()


def test_airflow_down_is_unavailable(monkeypatch, ctx):
    def refused(*a, **k):
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(af.requests, "post", refused)
    af._tokens.clear()
    r = execute_tool("airflow_list_dag_runs", {}, ctx)
    assert r.status is ToolStatus.UNAVAILABLE


def test_invalid_dag_id_is_validation_error(ctx):
    r = execute_tool("airflow_list_dag_runs", {"dag_id": "../../admin"}, ctx)
    assert r.status is ToolStatus.ERROR
    assert r.error.type == "ValidationError"
