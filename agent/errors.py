"""Typed failures raised *inside* tools. The registry maps each one to a ``ToolResult`` status,
so nothing ever propagates to the orchestrator as an exception (CLAUDE.md §4)."""

from __future__ import annotations

from typing import Any


class ToolFailure(Exception):
    """Base class; ``status`` is the ToolResult status this failure maps to."""

    status = "error"


class ServiceUnavailableError(ToolFailure):
    """Backend (MLflow, Airflow, Kubernetes API, git/dvc binary) cannot be reached."""

    status = "unavailable"


class PermissionDeniedError(ToolFailure):
    """Request violates a safety rule (namespace, DAG allow-list, command allow-list, limits)."""

    status = "rejected"


class InvalidRequestError(ToolFailure):
    """Input is well-formed but refers to something that does not exist or makes no sense."""

    status = "error"


class ConfirmationRequiredError(ToolFailure):
    """A state-changing action needs a human 'yes' collected by the orchestrator (never by the LLM)."""

    status = "confirmation_required"

    def __init__(self, summary: str, action: dict[str, Any]) -> None:
        super().__init__(summary)
        self.summary = summary
        self.action = action
