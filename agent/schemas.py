"""Shared data contracts: tool input base class, the ``ToolResult`` envelope, and the agent's final response
(AGENT.md §4). Each tool's own input model subclasses ``ToolInput`` and lives next to the tool."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ToolStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    REJECTED = "rejected"
    CONFIRMATION_REQUIRED = "confirmation_required"


class Permission(StrEnum):
    READ = "R"
    WRITE = "W"
    WRITE_CONFIRM = "WC"


class ToolInput(BaseModel):
    """Base for every tool's arguments.

    ``extra="forbid"``: the LLM cannot smuggle in fields the tool does not declare — in particular
    it can never pass something like ``confirmed=true``; confirmation is an orchestrator-only
    parameter (DECISION D13).
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class NoInput(ToolInput):
    """For tools that take no arguments."""


class ToolError(BaseModel):
    type: str
    message: str


class ToolResult(BaseModel):
    tool: str
    request_id: str
    status: ToolStatus
    data: dict[str, Any] = Field(default_factory=dict)
    error: ToolError | None = None
    duration_ms: int = 0
    evidence_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is ToolStatus.OK

    def for_llm(self, max_chars: int = 12_000) -> dict[str, Any]:
        """Compact payload sent back to the model (bounded so free-tier token limits are respected)."""
        payload = self.model_dump(mode="json", exclude={"request_id"}, exclude_none=True)
        action = payload.get("data", {}).get("action")
        if isinstance(action, dict):  # the approval token is for the orchestrator only, never the LLM
            action.pop("confirmation_token", None)
        text = str(payload)
        if len(text) > max_chars:
            payload["data"] = {"truncated": True, "preview": text[:max_chars]}
        return payload


class EvidenceRow(BaseModel):
    evidence_id: str
    tool: str
    status: ToolStatus
    summary: str
    duration_ms: int


class AgentResponse(BaseModel):
    request_id: str
    answer: str
    evidence: list[EvidenceRow] = Field(default_factory=list)
    tool_calls: int = 0
    iterations: int = 0
    stopped_reason: str = "completed"
