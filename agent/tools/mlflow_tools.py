"""MLflow tools (AGENT.md §3.2) — read-only, via ``MlflowClient`` only.

MLflow's HTTP client retries for a long time by default; the agent caps retries and request timeout so an
unreachable server surfaces quickly as ``status="unavailable"`` instead of hanging the conversation.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from typing import Any

from pydantic import Field, field_validator

from agent.errors import InvalidRequestError, ServiceUnavailableError
from agent.schemas import NoInput, ToolInput
from agent.tools.base import ToolContext, tool

SYSTEM = "mlflow"
KEY_METRICS = ("f1", "roc_auc", "accuracy", "precision", "recall")
KEY_TAGS = ("data.version", "model.type", "quality_gate", "promoted", "pipeline.run_id", "data.dvc_md5")
REGISTERED_MODEL = "churn-classifier"
_RUN_ID = re.compile(r"^[0-9a-f]{32}$")

os.environ.setdefault("MLFLOW_HTTP_REQUEST_MAX_RETRIES", "0")
os.environ.setdefault("MLFLOW_HTTP_REQUEST_TIMEOUT", "10")
os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")

_UNAVAILABLE_HINTS = ("max retries", "connection", "failed to establish", "timed out", "read timeout")


# --- client helpers -----------------------------------------------------------------------------------------


def get_client(ctx: ToolContext) -> Any:
    from mlflow import MlflowClient

    return MlflowClient(tracking_uri=ctx.settings.mlflow_tracking_uri)


def translate(exc: Exception, ctx: ToolContext) -> Exception:
    """Map MLflow exceptions onto the agent's typed failures."""
    message = str(exc)
    lowered = message.lower()
    if "resource_does_not_exist" in lowered or "not found" in lowered:
        return InvalidRequestError(message[:400])
    if any(hint in lowered for hint in _UNAVAILABLE_HINTS):
        return ServiceUnavailableError(f"MLflow server unreachable at {ctx.settings.mlflow_tracking_uri}")
    return exc


def call(ctx: ToolContext, fn: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except InvalidRequestError:
        raise
    except Exception as exc:
        raise translate(exc, ctx) from exc


def get_run_tags(run_id: str, ctx: ToolContext) -> dict[str, str]:
    """User tags of a run (``mlflow.*`` system tags removed). Shared with ``dvc_get_run_dataset``."""
    run = call(ctx, get_client(ctx).get_run, run_id)
    return {k: v for k, v in run.data.tags.items() if not k.startswith("mlflow.")}


def _iso(ms: int | None) -> str | None:
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat(timespec="seconds") if ms else None


def _age_days(ms: int | None) -> float | None:
    return round((datetime.now(UTC).timestamp() * 1000 - ms) / 86_400_000, 2) if ms else None


def _experiment_ids(client: Any, ctx: ToolContext, names: list[str] | None) -> dict[str, str]:
    """Map experiment id -> name for the requested names (all non-default experiments when None)."""
    experiments = call(ctx, client.search_experiments)
    mapping = {e.experiment_id: e.name for e in experiments if e.name != "Default"}
    if not names:
        return mapping
    wanted = {e_id: n for e_id, n in mapping.items() if n in names}
    missing = set(names) - set(wanted.values())
    if missing:
        available = sorted(mapping.values())
        raise InvalidRequestError(f"unknown experiment(s): {sorted(missing)}; available: {available}")
    return wanted


def _versions_by_run(client: Any, ctx: ToolContext, model: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    try:
        registered = call(ctx, client.get_registered_model, model)
    except InvalidRequestError:
        return out
    alias_of: dict[str, list[str]] = {}
    for alias, version in (registered.aliases or {}).items():
        alias_of.setdefault(str(version), []).append(alias)
    for mv in call(ctx, client.search_model_versions, f"name='{model}'"):
        out[mv.run_id] = {"model_version": mv.version, "aliases": sorted(alias_of.get(str(mv.version), []))}
    return out


def _run_row(run: Any, exp_names: dict[str, str], versions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    tags = run.data.tags
    return {
        "run_id": run.info.run_id,
        "run_name": run.info.run_name,
        "experiment": exp_names.get(run.info.experiment_id, run.info.experiment_id),
        "status": run.info.status,
        "start_time": _iso(run.info.start_time),
        "age_days": _age_days(run.info.start_time),
        "metrics": {k: run.data.metrics[k] for k in KEY_METRICS if k in run.data.metrics},
        "tags": {k: tags[k] for k in KEY_TAGS if k in tags},
        "registry": versions.get(run.info.run_id),
    }


# --- tools --------------------------------------------------------------------------------------------------


@tool(
    name="mlflow_list_experiments",
    system=SYSTEM,
    description="List MLflow experiments with id, run count and most recent run time.",
    input_model=NoInput,
)
def mlflow_list_experiments(args: NoInput, ctx: ToolContext) -> dict[str, Any]:
    client = get_client(ctx)
    rows = []
    for exp_id, name in _experiment_ids(client, ctx, None).items():
        runs = call(
            ctx, client.search_runs, [exp_id], max_results=1000, order_by=["attributes.start_time DESC"]
        )
        latest = _iso(runs[0].info.start_time) if runs else None
        rows.append({"experiment_id": exp_id, "name": name, "run_count": len(runs), "latest_run": latest})
    return {"tracking_uri": ctx.settings.mlflow_tracking_uri, "experiments": rows}


class ListRunsIn(ToolInput):
    experiment_names: list[str] | None = Field(default=None, description="Experiment names; all if omitted")
    filter_string: str | None = Field(
        default=None,
        max_length=300,
        description="MLflow search filter, e.g. metrics.f1 > 0.8 and tags.quality_gate = 'passed'",
    )
    order_by: str = Field(
        default="attributes.start_time DESC", max_length=80, description="e.g. 'metrics.f1 DESC'"
    )
    max_results: int = Field(default=10, ge=1, le=50)


@tool(
    name="mlflow_list_runs",
    system=SYSTEM,
    description="List runs (newest first by default) with key metrics, provenance tags and registry info.",
    input_model=ListRunsIn,
)
def mlflow_list_runs(args: ListRunsIn, ctx: ToolContext) -> dict[str, Any]:
    client = get_client(ctx)
    exps = _experiment_ids(client, ctx, args.experiment_names)
    runs = call(
        ctx,
        client.search_runs,
        list(exps),
        filter_string=args.filter_string or "",
        order_by=[args.order_by],
        max_results=args.max_results,
    )
    versions = _versions_by_run(client, ctx, REGISTERED_MODEL)
    return {"count": len(runs), "runs": [_run_row(r, exps, versions) for r in runs]}


class RunIdIn(ToolInput):
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$", description="MLflow run id (32 hex chars)")


@tool(
    name="mlflow_get_run",
    system=SYSTEM,
    description="Full detail of one run: params, metrics, provenance tags, artifacts, registered version.",
    input_model=RunIdIn,
)
def mlflow_get_run(args: RunIdIn, ctx: ToolContext) -> dict[str, Any]:
    client = get_client(ctx)
    run = call(ctx, client.get_run, args.run_id)
    exp = call(ctx, client.get_experiment, run.info.experiment_id)
    artifacts = [a.path for a in call(ctx, client.list_artifacts, args.run_id)]
    if "evaluation" in artifacts:
        artifacts += [a.path for a in call(ctx, client.list_artifacts, args.run_id, "evaluation")]
    return {
        "run_id": args.run_id,
        "run_name": run.info.run_name,
        "experiment": exp.name,
        "status": run.info.status,
        "start_time": _iso(run.info.start_time),
        "age_days": _age_days(run.info.start_time),
        "params": dict(run.data.params),
        "metrics": dict(run.data.metrics),
        "tags": {k: v for k, v in run.data.tags.items() if not k.startswith("mlflow.")},
        "artifacts": artifacts,
        "registry": _versions_by_run(client, ctx, REGISTERED_MODEL).get(args.run_id),
    }


class BestRunIn(ToolInput):
    metric: str = Field(default="f1", pattern=r"^[A-Za-z0-9_.]+$")
    direction: str = Field(default="max", pattern=r"^(max|min)$")
    experiment_names: list[str] | None = None
    data_version: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9._\-]+$", description="e.g. data-v2.0"
    )
    only_passed_gate: bool = Field(default=False, description="Only runs tagged quality_gate=passed")
    max_age_days: float | None = Field(default=None, gt=0, description="Only runs newer than this ('recent')")
    top_k: int = Field(default=3, ge=1, le=10, description="Best run plus runner-ups")


@tool(
    name="mlflow_best_run",
    system=SYSTEM,
    description=(
        "Best run by a metric (default f1, max) with runner-ups; optional filters for dataset version, "
        "quality-gate status and recency. Rows include gate status, dataset version and registry aliases."
    ),
    input_model=BestRunIn,
)
def mlflow_best_run(args: BestRunIn, ctx: ToolContext) -> dict[str, Any]:
    client = get_client(ctx)
    exps = _experiment_ids(client, ctx, args.experiment_names)
    clauses = ["attributes.status = 'FINISHED'"]
    if args.data_version:
        clauses.append(f"tags.`data.version` = '{args.data_version}'")
    if args.only_passed_gate:
        clauses.append("tags.quality_gate = 'passed'")
    if args.max_age_days:
        cutoff = int(datetime.now(UTC).timestamp() * 1000 - args.max_age_days * 86_400_000)
        clauses.append(f"attributes.start_time >= {cutoff}")
    order = "DESC" if args.direction == "max" else "ASC"
    runs = call(
        ctx,
        client.search_runs,
        list(exps),
        filter_string=" and ".join(clauses),
        order_by=[f"metrics.{args.metric} {order}", "attributes.start_time DESC"],
        max_results=200,
    )
    runs = [r for r in runs if args.metric in r.data.metrics][: args.top_k]
    if not runs:
        return {"found": False, "metric": args.metric, "filters": clauses}
    versions = _versions_by_run(client, ctx, REGISTERED_MODEL)
    rows = [_run_row(r, exps, versions) for r in runs]
    return {
        "found": True,
        "metric": args.metric,
        "direction": args.direction,
        "filters": clauses,
        "best": rows[0],
        "runner_ups": rows[1:],
    }


class CompareIn(ToolInput):
    run_ids: list[str] = Field(min_length=2, max_length=10)

    @field_validator("run_ids")
    @classmethod
    def _valid_ids(cls, v: list[str]) -> list[str]:
        bad = [r for r in v if not _RUN_ID.match(r)]
        if bad:
            raise ValueError(f"invalid run ids: {bad}")
        return v


@tool(
    name="mlflow_compare_runs",
    system=SYSTEM,
    description="Side-by-side comparison table of metrics, params and provenance tags for 2-10 runs.",
    input_model=CompareIn,
)
def mlflow_compare_runs(args: CompareIn, ctx: ToolContext) -> dict[str, Any]:
    client = get_client(ctx)
    runs = [call(ctx, client.get_run, rid) for rid in args.run_ids]
    metric_keys = sorted({k for r in runs for k in r.data.metrics})
    param_keys = sorted({k for r in runs for k in r.data.params})

    def row(source: str, key: str) -> dict[str, Any]:
        return {r.info.run_id[:8]: getattr(r.data, source).get(key) for r in runs}

    best = max(runs, key=lambda r: r.data.metrics.get("f1", float("-inf")))
    return {
        "columns": {r.info.run_id[:8]: r.info.run_id for r in runs},
        "table": {
            "metrics": {k: row("metrics", k) for k in metric_keys},
            "params": {k: row("params", k) for k in param_keys},
            "tags": {k: row("tags", k) for k in KEY_TAGS},
        },
        "best_by_f1": best.info.run_id,
    }


class ModelIn(ToolInput):
    model_name: str = Field(default=REGISTERED_MODEL, pattern=r"^[A-Za-z0-9._\-]+$")
    max_versions: int = Field(default=10, ge=1, le=50)


@tool(
    name="mlflow_get_model_versions",
    system=SYSTEM,
    description="Registered model versions (newest first) with aliases (champion/candidate), run, tags.",
    input_model=ModelIn,
)
def mlflow_get_model_versions(args: ModelIn, ctx: ToolContext) -> dict[str, Any]:
    client = get_client(ctx)
    registered = call(ctx, client.get_registered_model, args.model_name)
    aliases = {a: str(v) for a, v in (registered.aliases or {}).items()}
    versions = call(ctx, client.search_model_versions, f"name='{args.model_name}'")
    versions = sorted(versions, key=lambda mv: int(mv.version), reverse=True)[: args.max_versions]
    rows = [
        {
            "version": mv.version,
            "aliases": sorted(a for a, v in aliases.items() if v == str(mv.version)),
            "run_id": mv.run_id,
            "status": mv.status,
            "created": _iso(mv.creation_timestamp),
            "age_days": _age_days(mv.creation_timestamp),
            "tags": dict(mv.tags or {}),
        }
        for mv in versions
    ]
    return {"model": args.model_name, "aliases": aliases, "versions": rows}
