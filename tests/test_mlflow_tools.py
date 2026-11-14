"""S10: MLflow tools against an in-memory fake MlflowClient (no server)."""

from __future__ import annotations

import time
from types import SimpleNamespace as NS

import pytest

import agent.tools.mlflow_tools as mt
from agent.config import Settings, load_policy
from agent.schemas import ToolStatus
from agent.tools.base import ToolContext, execute_tool

NOW_MS = int(time.time() * 1000)
DAY = 86_400_000


def make_run(run_id: str, exp: str, f1: float, version: str, gate: str, age_days: float = 1.0):
    return NS(
        info=NS(
            run_id=run_id,
            run_name=f"run-{run_id[:4]}",
            experiment_id=exp,
            status="FINISHED",
            start_time=NOW_MS - int(age_days * DAY),
        ),
        data=NS(
            metrics={"f1": f1, "roc_auc": 0.86},
            params={"model_type": "gradient_boosting"},
            tags={"data.version": version, "quality_gate": gate, "mlflow.user": "x"},
        ),
    )


RUNS = [
    make_run("a" * 32, "1", 0.81, "data-v2.0", "passed", age_days=1),
    make_run("b" * 32, "1", 0.83, "data-v1.0", "failed", age_days=40),
    make_run("c" * 32, "2", 0.79, "data-v2.0", "passed", age_days=2),
]


class FakeClient:
    def __init__(self, runs=RUNS):
        self.runs = {r.info.run_id: r for r in runs}

    def search_experiments(self):
        return [NS(experiment_id="0", name="Default"), NS(experiment_id="1", name="churn-training"),
                NS(experiment_id="2", name="churn-hyperparam-search")]  # fmt: skip

    def search_runs(self, exp_ids, filter_string="", order_by=None, max_results=100):
        runs = [r for r in self.runs.values() if r.info.experiment_id in exp_ids]
        if "data-v2.0" in filter_string:
            runs = [r for r in runs if r.data.tags["data.version"] == "data-v2.0"]
        if "quality_gate = 'passed'" in filter_string:
            runs = [r for r in runs if r.data.tags["quality_gate"] == "passed"]
        if "start_time >=" in filter_string:
            cutoff = int(filter_string.split("start_time >= ")[1].split()[0])
            runs = [r for r in runs if r.info.start_time >= cutoff]
        if order_by and order_by[0].startswith("metrics.f1"):
            runs.sort(key=lambda r: r.data.metrics["f1"], reverse="DESC" in order_by[0])
        else:
            runs.sort(key=lambda r: r.info.start_time, reverse=True)
        return runs[:max_results]

    def get_run(self, run_id):
        if run_id not in self.runs:
            raise Exception(f"RESOURCE_DOES_NOT_EXIST: Run '{run_id}' not found")
        return self.runs[run_id]

    def get_experiment(self, exp_id):
        return NS(name={"1": "churn-training", "2": "churn-hyperparam-search"}[exp_id])

    def list_artifacts(self, run_id, path=None):
        return [NS(path="evaluation")] if path is None else [NS(path="evaluation/confusion_matrix.json")]

    def get_registered_model(self, name):
        return NS(aliases={"champion": "7", "candidate": "8"})

    def search_model_versions(self, filter_string):
        gate = {"quality_gate": "passed"}
        return [
            NS(version="7", run_id="a" * 32, status="READY", creation_timestamp=NOW_MS, tags=gate),
            NS(version="8", run_id="c" * 32, status="READY", creation_timestamp=NOW_MS, tags={}),
        ]


@pytest.fixture
def ctx(monkeypatch) -> ToolContext:
    monkeypatch.setattr(mt, "get_client", lambda c: FakeClient())
    return ToolContext(request_id="req-ml", settings=Settings(_env_file=None), policy=load_policy())


def test_best_run_by_f1_with_runner_ups(ctx):
    r = execute_tool("mlflow_best_run", {}, ctx)
    assert r.status is ToolStatus.OK
    assert r.data["best"]["run_id"] == "b" * 32  # highest raw F1...
    assert r.data["best"]["tags"]["quality_gate"] == "failed"  # ...but it failed the gate
    assert [x["run_id"] for x in r.data["runner_ups"]] == ["a" * 32, "c" * 32]


def test_best_run_respects_gate_and_recency_filters(ctx):
    r = execute_tool("mlflow_best_run", {"only_passed_gate": True, "max_age_days": 30}, ctx)
    assert r.data["best"]["run_id"] == "a" * 32
    assert r.data["best"]["registry"] == {"model_version": "7", "aliases": ["champion"]}


def test_best_run_none_found(ctx, monkeypatch):
    monkeypatch.setattr(mt, "get_client", lambda c: FakeClient(runs=[]))
    r = execute_tool("mlflow_best_run", {}, ctx)
    assert r.status is ToolStatus.OK
    assert r.data["found"] is False


def test_list_experiments_excludes_default(ctx):
    r = execute_tool("mlflow_list_experiments", {}, ctx)
    names = {e["name"]: e["run_count"] for e in r.data["experiments"]}
    assert names == {"churn-training": 2, "churn-hyperparam-search": 1}


def test_list_runs_unknown_experiment_is_error(ctx):
    r = execute_tool("mlflow_list_runs", {"experiment_names": ["nope"]}, ctx)
    assert r.status is ToolStatus.ERROR
    assert "unknown experiment" in r.error.message


def test_get_run_hides_system_tags_and_lists_artifacts(ctx):
    r = execute_tool("mlflow_get_run", {"run_id": "a" * 32}, ctx)
    assert "mlflow.user" not in r.data["tags"]
    assert "evaluation/confusion_matrix.json" in r.data["artifacts"]


def test_get_run_missing_is_error_not_crash(ctx):
    r = execute_tool("mlflow_get_run", {"run_id": "d" * 32}, ctx)
    assert r.status is ToolStatus.ERROR


def test_compare_runs_table(ctx):
    r = execute_tool("mlflow_compare_runs", {"run_ids": ["a" * 32, "c" * 32]}, ctx)
    assert r.data["table"]["metrics"]["f1"] == {"aaaaaaaa": 0.81, "cccccccc": 0.79}
    assert r.data["best_by_f1"] == "a" * 32


def test_compare_runs_rejects_bad_ids(ctx):
    r = execute_tool("mlflow_compare_runs", {"run_ids": ["a" * 32, "not-a-run"]}, ctx)
    assert r.status is ToolStatus.ERROR
    assert r.error.type == "ValidationError"


def test_model_versions_with_aliases(ctx):
    r = execute_tool("mlflow_get_model_versions", {}, ctx)
    assert r.data["aliases"] == {"champion": "7", "candidate": "8"}
    assert r.data["versions"][0]["version"] == "8"  # newest first


def test_server_down_is_unavailable(monkeypatch):
    class Down:
        def search_experiments(self):
            raise Exception("HTTPConnectionPool: Max retries exceeded (Failed to establish a new connection)")

    monkeypatch.setattr(mt, "get_client", lambda c: Down())
    ctx = ToolContext(request_id="req-down", settings=Settings(_env_file=None), policy=load_policy())
    r = execute_tool("mlflow_list_experiments", {}, ctx)
    assert r.status is ToolStatus.UNAVAILABLE
    assert "unreachable" in r.error.message
