"""Tool registry: ``@tool`` registration, LLM schema export, and the single execution path every tool call
takes (validate → run with timeout → map failures → envelope → structured log). Nothing raises out of
``execute_tool`` (CLAUDE.md §4, DECISION D3)."""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from agent.config import Policy, Settings, get_settings, load_policy
from agent.errors import ConfirmationRequiredError, ToolFailure
from agent.logging_utils import get_logger, redact, redact_obj
from agent.schemas import Permission, ToolError, ToolInput, ToolResult, ToolStatus

log = get_logger("tools")

_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="tool")


@dataclass
class ToolContext:
    """Per-call context. ``confirmation_token`` is set only by the orchestrator after a human 'yes'."""

    request_id: str
    settings: Settings = field(default_factory=get_settings)
    policy: Policy = field(default_factory=load_policy)
    confirmation_token: str | None = None


ToolFn = Callable[[Any, ToolContext], dict[str, Any]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    system: str
    description: str
    input_model: type[ToolInput]
    permission: Permission
    fn: ToolFn
    timeout_seconds: float | None = None

    def llm_declaration(self) -> dict[str, Any]:
        """Function declaration for the LLM (name, description, cleaned JSON schema)."""
        return {
            "name": self.name,
            "description": f"[{self.system}, {self.permission.value}] {self.description}",
            "parameters": clean_schema(self.input_model.model_json_schema()),
        }


REGISTRY: dict[str, ToolSpec] = {}


def tool(
    *,
    name: str,
    system: str,
    description: str,
    input_model: type[ToolInput],
    permission: Permission = Permission.READ,
    timeout_seconds: float | None = None,
) -> Callable[[ToolFn], ToolFn]:
    """Register a tool function ``fn(args: input_model, ctx: ToolContext) -> dict``."""

    def decorator(fn: ToolFn) -> ToolFn:
        if name in REGISTRY:
            raise ValueError(f"duplicate tool name: {name}")
        REGISTRY[name] = ToolSpec(name, system, description, input_model, permission, fn, timeout_seconds)
        return fn

    return decorator


def clean_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline ``$defs`` and drop keys Gemini's function-calling schema does not accept."""
    defs = schema.get("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(defs[node["$ref"].split("/")[-1]])
            if "anyOf" in node:  # Optional[X] -> X (nullable handled by 'required')
                options = [o for o in node["anyOf"] if o.get("type") != "null"]
                merged = {**walk(options[0]), **{k: v for k, v in node.items() if k not in {"anyOf"}}}
                return walk(merged)
            return {
                k: walk(v)
                for k, v in node.items()
                if k not in {"title", "$defs", "additionalProperties", "default"}
            }
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    cleaned = walk(schema)
    cleaned.setdefault("type", "object")
    cleaned.setdefault("properties", {})
    return cleaned


def declarations() -> list[dict[str, Any]]:
    return [spec.llm_declaration() for spec in REGISTRY.values()]


def _error(spec_name: str, ctx: ToolContext, status: ToolStatus, exc_type: str, message: str) -> ToolResult:
    return ToolResult(
        tool=spec_name,
        request_id=ctx.request_id,
        status=status,
        error=ToolError(type=exc_type, message=redact(message)),
    )


def execute_tool(name: str, raw_args: dict[str, Any] | None, ctx: ToolContext) -> ToolResult:
    """Validate, execute with timeout and wrap the outcome. Never raises."""
    started = time.perf_counter()
    spec = REGISTRY.get(name)
    if spec is None:
        result = _error(name, ctx, ToolStatus.ERROR, "UnknownTool", f"no tool named '{name}'")
    else:
        result = _run(spec, raw_args or {}, ctx)
    result.duration_ms = int((time.perf_counter() - started) * 1000)
    log.info(
        "tool call",
        extra={
            "request_id": ctx.request_id,
            "tool": name,
            "status": result.status.value,
            "duration_ms": result.duration_ms,
            "error_type": result.error.type if result.error else None,
        },
    )
    return result


def _run(spec: ToolSpec, raw_args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    try:
        args = spec.input_model.model_validate(raw_args)
    except ValidationError as exc:
        details = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'input'}: {e['msg']}" for e in exc.errors())
        return _error(spec.name, ctx, ToolStatus.ERROR, "ValidationError", f"invalid arguments: {details}")

    timeout = spec.timeout_seconds or ctx.settings.agent_tool_timeout_seconds
    future = _EXECUTOR.submit(spec.fn, args, ctx)
    try:
        data = future.result(timeout=timeout)
    except FutureTimeout:
        future.cancel()
        return _error(spec.name, ctx, ToolStatus.TIMEOUT, "Timeout", f"{spec.name} exceeded {timeout:g}s")
    except ConfirmationRequiredError as exc:
        return ToolResult(
            tool=spec.name,
            request_id=ctx.request_id,
            status=ToolStatus.CONFIRMATION_REQUIRED,
            data={"summary": exc.summary, "action": exc.action},
        )
    except ToolFailure as exc:
        return _error(spec.name, ctx, ToolStatus(exc.status), type(exc).__name__, str(exc))
    except (ConnectionError, OSError) as exc:
        return _error(spec.name, ctx, ToolStatus.UNAVAILABLE, type(exc).__name__, str(exc))
    except Exception as exc:
        return _error(spec.name, ctx, ToolStatus.ERROR, type(exc).__name__, str(exc))

    return ToolResult(tool=spec.name, request_id=ctx.request_id, status=ToolStatus.OK, data=redact_obj(data))
