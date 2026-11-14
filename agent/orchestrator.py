"""Tool-use loop (AGENT.md §5, DECISIONS D3, D16, D18, D19).

1. assign ``request_id``; 2. ask the LLM with the system prompt, history and tool declarations;
3. execute every requested tool call (independent calls concurrently) through ``execute_tool`` — validation,
   permission checks, timeout, error mapping and logging all happen there; 4. pause for a human on
   ``confirmation_required``; 5. repeat until the LLM answers or the iteration/time budget is exhausted;
6. append a deterministic Evidence table built from the real ``ToolResult`` objects, plus a grounding check
   that flags figures in the answer that appear in no tool result.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from agent.config import Policy, Settings, get_settings, load_policy
from agent.llm import LLMClient, ToolCall
from agent.logging_utils import get_logger, register_secrets, request_id_var
from agent.prompts import system_prompt
from agent.schemas import AgentResponse, EvidenceRow, Permission, ToolError, ToolResult, ToolStatus
from agent.tools import load_all
from agent.tools.base import REGISTRY, ToolContext, declarations, execute_tool

log = get_logger("orchestrator")

ConfirmFn = Callable[[str, dict[str, Any]], bool]


def deny_all(summary: str, action: dict[str, Any]) -> bool:
    """Default confirmation handler for non-interactive use: never approve."""
    return False


@dataclass
class Turn:
    """Everything that happened while answering one user message (used for transcripts and tests)."""

    question: str
    response: AgentResponse | None = None
    results: list[ToolResult] = field(default_factory=list)
    calls: list[ToolCall] = field(default_factory=list)
    confirmations: list[dict[str, Any]] = field(default_factory=list)


class Orchestrator:
    def __init__(
        self,
        llm: LLMClient,
        settings: Settings | None = None,
        policy: Policy | None = None,
        confirm: ConfirmFn = deny_all,
        time_budget_s: float = 240.0,
    ) -> None:
        load_all()
        self.llm = llm
        self.settings = settings or get_settings()
        self.policy = policy or load_policy(self.settings.policy_path)
        self.confirm = confirm
        self.time_budget_s = time_budget_s
        self.history: list[dict[str, Any]] = []
        self.system = system_prompt(self.settings, self.policy)
        self.tools = declarations()
        register_secrets(*self.settings.secret_values())

    # -- public -------------------------------------------------------------------------------------------

    def ask(self, question: str) -> Turn:
        request_id = f"req-{uuid.uuid4().hex[:10]}"
        token = request_id_var.set(request_id)
        turn = Turn(question=question)
        started = time.monotonic()
        log.info("request", extra={"question_chars": len(question)})
        try:
            self._question = question
            self.history.append({"role": "user", "text": question})
            text, iterations, stopped = "", 0, "completed"
            while True:
                if iterations >= self.settings.agent_max_iterations:
                    stopped = "max_iterations"
                    break
                if time.monotonic() - started > self.time_budget_s:
                    stopped = "time_budget"
                    break
                iterations += 1
                try:
                    reply = self.llm.generate(self.system, self.history, self.tools)
                except Exception as exc:  # LLM down / quota: report honestly, never invent
                    log.error("llm failure", extra={"error_type": type(exc).__name__})
                    text, stopped = (
                        f"The language model could not be reached ({type(exc).__name__}: {exc}).",
                        "llm_error",
                    )
                    break
                self.history.append(
                    {
                        "role": "assistant",
                        "text": reply.text,
                        "tool_calls": reply.tool_calls,
                        "raw": reply.raw,
                    }
                )
                if not reply.tool_calls:
                    text = reply.text
                    break
                results = self._run_calls(reply.tool_calls, request_id, turn)
                self.history.append(
                    {
                        "role": "tool",
                        "results": [(c, r.for_llm()) for c, r in zip(reply.tool_calls, results, strict=True)],
                    }
                )
            if stopped in ("max_iterations", "time_budget") and not text:
                text = (
                    f"Stopped before a final answer ({stopped.replace('_', ' ')} reached). "
                    "Partial evidence is listed below."
                )
            answer = self._finalize(
                text, turn.results, check_grounding=stopped != "llm_error", question=question
            )
            turn.response = AgentResponse(
                request_id=request_id,
                answer=answer,
                evidence=[self._row(r) for r in turn.results],
                tool_calls=len(turn.results),
                iterations=iterations,
                stopped_reason=stopped,
            )
            log.info(
                "response",
                extra={"tool_calls": len(turn.results), "iterations": iterations, "stopped_reason": stopped},
            )
            return turn
        finally:
            request_id_var.reset(token)

    # -- tool execution -----------------------------------------------------------------------------------

    def _run_calls(self, calls: list[ToolCall], request_id: str, turn: Turn) -> list[ToolResult]:
        def run(call: ToolCall) -> ToolResult:
            mismatch = intent_mismatch(getattr(self, "_question", ""), call, self.settings.k8s_namespace)
            if mismatch:
                log.warning("intent mismatch", extra={"tool": call.name})
                return ToolResult(
                    tool=call.name,
                    request_id=request_id,
                    status=ToolStatus.REJECTED,
                    error=ToolError(type="IntentMismatch", message=mismatch),
                )
            return execute_tool(call.name, call.args, ToolContext(request_id, self.settings, self.policy))

        with ThreadPoolExecutor(max_workers=min(8, len(calls))) as pool:
            results = list(pool.map(run, calls))
        final = []
        for call, result in zip(calls, results, strict=True):
            if result.status is ToolStatus.CONFIRMATION_REQUIRED:
                result = self._confirm_and_run(call, result, request_id, turn)
            result.evidence_id = f"E{len(turn.results) + 1}"
            turn.results.append(result)
            turn.calls.append(call)
            final.append(result)
        return final

    def _confirm_and_run(
        self, call: ToolCall, pending: ToolResult, request_id: str, turn: Turn
    ) -> ToolResult:
        """Human-in-the-loop: the token never leaves the orchestrator; the LLM cannot approve anything."""
        summary = pending.data.get("summary", call.name)
        action = pending.data.get("action", {})
        approved = bool(self.confirm(summary, {k: v for k, v in action.items() if k != "confirmation_token"}))
        turn.confirmations.append({"tool": call.name, "summary": summary, "approved": approved})
        log.info("confirmation", extra={"tool": call.name, "approved": approved})
        if not approved:
            return ToolResult(
                tool=call.name,
                request_id=request_id,
                status=ToolStatus.REJECTED,
                error=ToolError(type="DeclinedByUser", message=f"human declined: {summary}"),
            )
        ctx = ToolContext(
            request_id, self.settings, self.policy, confirmation_token=action.get("confirmation_token")
        )
        return execute_tool(call.name, call.args, ctx)

    # -- answer post-processing ---------------------------------------------------------------------------

    @staticmethod
    def _summary(result: ToolResult) -> str:
        if result.error:
            return f"{result.error.type}: {result.error.message}"[:140]
        keys = list(result.data)[:4]
        return ("keys: " + ", ".join(keys))[:140] if keys else "ok"

    def _row(self, result: ToolResult) -> EvidenceRow:
        return EvidenceRow(
            evidence_id=result.evidence_id or "?",
            tool=result.tool,
            status=result.status,
            summary=self._summary(result),
            duration_ms=result.duration_ms,
        )

    def _finalize(
        self, text: str, results: list[ToolResult], check_grounding: bool = True, question: str = ""
    ) -> str:
        parts = [text.strip() or "(no answer text)"]

        def recovered(i: int, r: ToolResult) -> bool:
            """A failed call followed by a successful call of the same tool (LLM corrected its input)."""
            return any(later.tool == r.tool and later.status is ToolStatus.OK for later in results[i + 1 :])

        failed = [r for i, r in enumerate(results) if r.status is not ToolStatus.OK and not recovered(i, r)]
        retried = [r for i, r in enumerate(results) if r.status is not ToolStatus.OK and recovered(i, r)]
        if retried:
            parts.append(
                "> **Corrected calls:** "
                + "; ".join(f"`{r.tool}` -> {r.status.value} [{r.evidence_id}]" for r in retried)
                + " (a later call of the same tool succeeded)."
            )
        if failed:
            systems = sorted({REGISTRY[r.tool].system if r.tool in REGISTRY else r.tool for r in failed})
            parts.append(
                "> **Not verified:** "
                + "; ".join(f"`{r.tool}` -> {r.status.value} [{r.evidence_id}]" for r in failed)
                + f". Facts from {', '.join(systems)} could not be confirmed by tools in this request."
            )
        known = {r.evidence_id for r in results}
        bogus = sorted(set(re.findall(r"E\d+", " ".join(_CITATION.findall(text)))) - known)
        if bogus:
            parts.append(
                "> **Citation check:** the answer cites evidence that does not exist: "
                + ", ".join(f"`{b}`" for b in bogus)
            )
        ungrounded = ungrounded_figures(text, results, question) if check_grounding else []
        if ungrounded:
            parts.append(
                "> **Grounding check:** these figures in the answer do not appear in any tool result: "
                + ", ".join(f"`{f}`" for f in ungrounded)
            )
        parts.append(evidence_table(results))
        return "\n\n".join(parts)


_FIGURE = re.compile(
    r"(?<![\w.])(?:v\d+|\d+\.\d+|\d+(?=\s*(?:replicas?|pods?|restarts?|runs?|versions?)\b))", re.I
)
_CITATION = re.compile(r"\[E\d+(?:\s*,\s*E\d+)*\]")


def ungrounded_figures(text: str, results: list[ToolResult], question: str = "") -> list[str]:
    """Numbers/versions in the answer found in no tool result payload (deterministic no-invention guard)."""
    # Allowed sources: successful payloads, error messages (e.g. "replicas=10 outside 1..5") and the
    # user's own question (figures the user stated are not invented by the model).
    corpus = " ".join(
        [json.dumps(r.data, default=str) for r in results if r.status is ToolStatus.OK]
        + [r.error.message for r in results if r.error]
        + [question]
    )
    figures = dict.fromkeys(_FIGURE.findall(_CITATION.sub("", text)))
    missing = []
    for fig in figures:
        bare = fig[1:] if fig.lower().startswith("v") else fig
        candidates = {fig, bare, f'"{bare}"', bare.rstrip("0").rstrip(".")}
        if not any(c and c in corpus for c in candidates):
            missing.append(fig)
    return missing


def evidence_table(results: list[ToolResult]) -> str:
    if not results:
        return "**Evidence:** no tools were called for this answer."
    lines = [
        "**Evidence (generated from tool results)**",
        "",
        "| ID | Tool | Status | ms | Detail |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        detail = Orchestrator._summary(r).replace("|", "/")
        lines.append(f"| {r.evidence_id} | `{r.tool}` | {r.status.value} | {r.duration_ms} | {detail} |")
    return "\n".join(lines)


_NAMESPACE_MENTION = re.compile(
    r"\bnamespaces?\s+[`'\"]?(?P<a>[a-z0-9][a-z0-9-]*)"
    r"|\b(?P<b>[a-z0-9][a-z0-9-]*)[`'\"]?\s+namespace\b"
    r"|(?:^|\s)-n\s+(?P<c>[a-z0-9][a-z0-9-]*)",
    re.I,
)
_NOT_NAMES = {"the", "a", "an", "this", "that", "same", "approved", "allowed", "any", "another", "other"}


def intent_mismatch(question: str, call: ToolCall, default_namespace: str) -> str | None:
    """Deterministic guard (DECISION D37): a state-changing call must not target a different namespace
    than the one the user explicitly named. LLMs sometimes substitute the allowed namespace; refuse that."""
    spec = REGISTRY.get(call.name)
    if spec is None or spec.permission is Permission.READ or "namespace" not in spec.input_model.model_fields:
        return None
    named = {
        (m.group("a") or m.group("b") or m.group("c")).lower() for m in _NAMESPACE_MENTION.finditer(question)
    } - _NOT_NAMES
    target = (call.args.get("namespace") or default_namespace).lower()
    if named and target not in named:
        return (
            f"request named namespace {sorted(named)} but the call targets '{target}'; refusing to "
            "substitute a different target - ask the user to confirm the intended namespace"
        )
    return None
