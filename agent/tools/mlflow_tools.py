"""MLflow tools (AGENT.md §3.2) — read-only, via ``MlflowClient`` only.

MLflow's HTTP client retries for a long time by default; the agent caps retries and request timeout so an
unreachable server surfaces quickly as ``status="unavailable"`` instead of hanging the conversation.
"""

from __future__ import annotations

import os
from typing import Any

from agent.errors import InvalidRequestError, ServiceUnavailableError
from agent.tools.base import ToolContext

SYSTEM = "mlflow"

os.environ.setdefault("MLFLOW_HTTP_REQUEST_MAX_RETRIES", "0")
os.environ.setdefault("MLFLOW_HTTP_REQUEST_TIMEOUT", "10")
os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")

_UNAVAILABLE_HINTS = ("max retries", "connection", "failed to establish", "timed out", "read timeout")


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
    except Exception as exc:
        raise translate(exc, ctx) from exc


def get_run_tags(run_id: str, ctx: ToolContext) -> dict[str, str]:
    """User tags of a run (``mlflow.*`` system tags removed). Shared with ``dvc_get_run_dataset``."""
    run = call(ctx, get_client(ctx).get_run, run_id)
    return {k: v for k, v in run.data.tags.items() if not k.startswith("mlflow.")}
