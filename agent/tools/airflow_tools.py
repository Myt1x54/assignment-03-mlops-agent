"""Airflow 3 tools (AGENT.md §3.3) — REST API v2 with JWT auth (``POST /auth/token``).

* The JWT is cached per base URL/user and refreshed once on HTTP 401.
* Credentials never appear in results or logs (``SecretStr`` + redaction); an auth failure is reported as
  ``status="error"`` with a generic message.
* ``airflow_trigger_dag`` only accepts DAG ids on the approved list and, like Kubernetes changes, needs a
  human confirmation collected by the orchestrator (DECISION D34).
"""

from __future__ import annotations

import re
import threading
from typing import Any

import requests
from pydantic import Field, field_validator

from agent.errors import InvalidRequestError, PermissionDeniedError, ServiceUnavailableError
from agent.safety import check_dag, clamp_tail, require_confirmation
from agent.schemas import Permission, ToolInput
from agent.tools.base import ToolContext, tool

SYSTEM = "airflow"
DEFAULT_DAG = "ml_training_pipeline"
_DAG_ID = r"^[A-Za-z0-9_.\-]{1,250}$"
_RUN_ID = r"^[A-Za-z0-9_.:+\-]{1,250}$"
_DATA_PATH = re.compile(r"^data/[A-Za-z0-9_\-/]+\.csv$")

_token_lock = threading.Lock()
_tokens: dict[tuple[str, str], str] = {}


class AirflowAuthError(InvalidRequestError):
    """Login rejected — message never includes the credentials."""


# --- HTTP client --------------------------------------------------------------------------------------------


def _timeout(ctx: ToolContext) -> float:
    return min(ctx.settings.agent_tool_timeout_seconds, 15.0)


def _login(ctx: ToolContext) -> str:
    s = ctx.settings
    try:
        resp = requests.post(
            f"{s.airflow_base_url}/auth/token",
            json={"username": s.airflow_username, "password": s.airflow_password.get_secret_value()},
            timeout=_timeout(ctx),
        )
    except requests.RequestException as exc:
        raise ServiceUnavailableError(f"Airflow API unreachable at {s.airflow_base_url}") from exc
    if resp.status_code in (401, 403):
        raise AirflowAuthError("Airflow authentication failed (check AIRFLOW_USERNAME / AIRFLOW_PASSWORD)")
    if resp.status_code >= 400:
        raise InvalidRequestError(f"Airflow login returned HTTP {resp.status_code}")
    return resp.json()["access_token"]


def _token(ctx: ToolContext, refresh: bool = False) -> str:
    key = (ctx.settings.airflow_base_url, ctx.settings.airflow_username)
    with _token_lock:
        if refresh or key not in _tokens:
            _tokens[key] = _login(ctx)
        return _tokens[key]


def api(ctx: ToolContext, method: str, path: str, **kwargs: Any) -> Any:
    """Authenticated call to ``/api/v2`` with one transparent token refresh."""
    url = f"{ctx.settings.airflow_base_url}/api/v2{path}"
    for attempt in range(2):
        headers = {
            "Authorization": f"Bearer {_token(ctx, refresh=attempt > 0)}",
            "Accept": "application/json",
        }
        try:
            resp = requests.request(method, url, headers=headers, timeout=_timeout(ctx), **kwargs)
        except requests.RequestException as exc:
            raise ServiceUnavailableError(
                f"Airflow API unreachable at {ctx.settings.airflow_base_url}"
            ) from exc
        if resp.status_code == 401 and attempt == 0:
            continue
        if resp.status_code == 404:
            raise InvalidRequestError(f"not found: {path}")
        if resp.status_code == 403:
            raise PermissionDeniedError(f"Airflow denied access to {path}")
        if resp.status_code >= 500:
            raise ServiceUnavailableError(f"Airflow returned HTTP {resp.status_code} for {path}")
        if resp.status_code >= 400:
            raise InvalidRequestError(f"Airflow returned HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json()
    raise AirflowAuthError("Airflow authentication failed after token refresh")


def _latest_run_id(ctx: ToolContext, dag_id: str) -> str:
    runs = api(ctx, "GET", f"/dags/{dag_id}/dagRuns", params={"limit": 1, "order_by": "-run_after"})[
        "dag_runs"
    ]
    if not runs:
        raise InvalidRequestError(f"DAG '{dag_id}' has no runs yet")
    return runs[0]["dag_run_id"]


def _run_row(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "dag_run_id": run.get("dag_run_id"),
        "state": run.get("state"),
        "run_type": run.get("run_type"),
        "run_after": run.get("run_after"),
        "start_date": run.get("start_date"),
        "end_date": run.get("end_date"),
        "duration_s": run.get("duration"),
        "conf": run.get("conf") or {},
        "triggered_by": run.get("triggered_by"),
    }


# --- inputs -------------------------------------------------------------------------------------------------


class ListDagsIn(ToolInput):
    limit: int = Field(default=25, ge=1, le=100)


class DagIn(ToolInput):
    dag_id: str = Field(default=DEFAULT_DAG, pattern=_DAG_ID)


class DagRunsIn(DagIn):
    limit: int = Field(default=5, ge=1, le=25)
    state: str | None = Field(default=None, pattern=r"^(queued|running|success|failed)$")


class TaskInstancesIn(DagIn):
    dag_run_id: str | None = Field(default=None, pattern=_RUN_ID, description="Defaults to the latest run")


class TaskLogsIn(DagIn):
    task_id: str = Field(pattern=r"^[A-Za-z0-9_.\-]{1,250}$")
    dag_run_id: str | None = Field(default=None, pattern=_RUN_ID, description="Defaults to the latest run")
    try_number: int | None = Field(default=None, ge=1, le=20, description="Defaults to the last try")
    tail_lines: int = Field(default=40, ge=1, le=500)


class TriggerIn(DagIn):
    data_path: str | None = Field(
        default=None, description="Optional dataset file inside the repo, e.g. data/incoming/churn_batch.csv"
    )
    note: str | None = Field(default=None, max_length=200)

    @field_validator("data_path")
    @classmethod
    def _safe_path(cls, v: str | None) -> str | None:
        if v is not None and (".." in v or not _DATA_PATH.match(v)):
            raise ValueError("data_path must be a .csv file under data/ (no '..')")
        return v


# --- tools --------------------------------------------------------------------------------------------------


@tool(
    name="airflow_list_dags",
    system=SYSTEM,
    description="List Airflow DAGs with paused state, schedule, tags and whether the agent may trigger them.",
    input_model=ListDagsIn,
)
def airflow_list_dags(args: ListDagsIn, ctx: ToolContext) -> dict[str, Any]:
    body = api(ctx, "GET", "/dags", params={"limit": args.limit})
    approved = ctx.settings.approved_dags
    dags = [
        {
            "dag_id": d["dag_id"],
            "is_paused": d.get("is_paused"),
            "schedule": d.get("timetable_summary") or d.get("timetable_description"),
            "tags": [t.get("name") if isinstance(t, dict) else t for t in d.get("tags") or []],
            "has_import_errors": d.get("has_import_errors"),
            "agent_may_trigger": d["dag_id"] in approved,
        }
        for d in body.get("dags", [])
    ]
    return {"total": body.get("total_entries", len(dags)), "dags": dags, "approved_dags": approved}


@tool(
    name="airflow_list_dag_runs",
    system=SYSTEM,
    description="Recent runs of a DAG (newest first) with state, timings, conf and run type.",
    input_model=DagRunsIn,
)
def airflow_list_dag_runs(args: DagRunsIn, ctx: ToolContext) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": args.limit, "order_by": "-run_after"}
    if args.state:
        params["state"] = args.state
    body = api(ctx, "GET", f"/dags/{args.dag_id}/dagRuns", params=params)
    runs = [_run_row(r) for r in body.get("dag_runs", [])]
    return {
        "dag_id": args.dag_id,
        "total_runs": body.get("total_entries", len(runs)),
        "runs": runs,
        "latest_state": runs[0]["state"] if runs else None,
    }


@tool(
    name="airflow_get_task_instances",
    system=SYSTEM,
    description=(
        "Task states for a DAG run (latest by default). Highlights failed and upstream_failed tasks and the "
        "first failing task in pipeline order."
    ),
    input_model=TaskInstancesIn,
)
def airflow_get_task_instances(args: TaskInstancesIn, ctx: ToolContext) -> dict[str, Any]:
    run_id = args.dag_run_id or _latest_run_id(ctx, args.dag_id)
    run = api(ctx, "GET", f"/dags/{args.dag_id}/dagRuns/{run_id}")
    body = api(ctx, "GET", f"/dags/{args.dag_id}/dagRuns/{run_id}/taskInstances")
    tasks = sorted(body.get("task_instances", []), key=lambda t: t.get("start_date") or "9999")
    rows = [
        {
            "task_id": t["task_id"],
            "state": t.get("state"),
            "try_number": t.get("try_number"),
            "max_tries": t.get("max_tries"),
            "start_date": t.get("start_date"),
            "end_date": t.get("end_date"),
            "duration_s": t.get("duration"),
        }
        for t in tasks
    ]
    failed = [r["task_id"] for r in rows if r["state"] == "failed"]
    return {
        "dag_id": args.dag_id,
        "dag_run_id": run_id,
        "run_state": run.get("state"),
        "conf": run.get("conf") or {},
        "tasks": rows,
        "failed_tasks": failed,
        "upstream_failed_tasks": [r["task_id"] for r in rows if r["state"] == "upstream_failed"],
        "first_failed_task": failed[0] if failed else None,
    }


def _log_lines(content: Any) -> list[str]:
    """Airflow 3 returns structured log events; older/text responses are plain strings."""
    if isinstance(content, str):
        return content.splitlines()
    lines: list[str] = []
    for item in content or []:
        if isinstance(item, dict):
            event = item.get("event", "")
            level = item.get("level")
            lines.append(f"[{level}] {event}" if level else str(event))
        else:
            lines.extend(str(item).splitlines())
    return lines


@tool(
    name="airflow_get_task_logs",
    system=SYSTEM,
    description="Log tail of one task try (latest run/try by default); secrets are redacted.",
    input_model=TaskLogsIn,
)
def airflow_get_task_logs(args: TaskLogsIn, ctx: ToolContext) -> dict[str, Any]:
    run_id = args.dag_run_id or _latest_run_id(ctx, args.dag_id)
    base = f"/dags/{args.dag_id}/dagRuns/{run_id}/taskInstances/{args.task_id}"
    try_number = args.try_number or api(ctx, "GET", base).get("try_number") or 1
    body = api(ctx, "GET", f"{base}/logs/{try_number}", params={"full_content": "true"})
    lines = [ln for ln in _log_lines(body.get("content")) if ln.strip()]
    tail = clamp_tail(args.tail_lines, ctx.policy.safety.log_tail_max_lines)
    errors = [
        ln for ln in lines if any(w in ln.lower() for w in ("error", "failed", "traceback", "exception"))
    ]
    return {
        "dag_run_id": run_id,
        "task_id": args.task_id,
        "try_number": try_number,
        "total_lines": len(lines),
        "error_lines": errors[-15:],
        "tail": lines[-tail:],
    }


@tool(
    name="airflow_trigger_dag",
    system=SYSTEM,
    description=(
        "Trigger an APPROVED DAG (ml_training_pipeline only). Use only when the user explicitly asks to run "
        "training. Requires human confirmation; optional data_path selects a dataset file under data/."
    ),
    input_model=TriggerIn,
    permission=Permission.WRITE_CONFIRM,
)
def airflow_trigger_dag(args: TriggerIn, ctx: ToolContext) -> dict[str, Any]:
    check_dag(args.dag_id, ctx.settings.approved_dags)
    conf = {"data_path": args.data_path} if args.data_path else {}
    action = {"dag_id": args.dag_id, "conf": conf}
    require_confirmation(
        tool="airflow_trigger_dag",
        args=action,
        token=ctx.confirmation_token,
        ttl_seconds=ctx.policy.safety.confirmation_ttl_seconds,
        summary=f"Trigger Airflow DAG '{args.dag_id}' with conf {conf or '{}'}",
    )
    payload: dict[str, Any] = {"logical_date": None, "conf": conf}
    if args.note:
        payload["note"] = args.note
    run = api(ctx, "POST", f"/dags/{args.dag_id}/dagRuns", json=payload)
    return {"triggered": True, **_run_row(run)}
