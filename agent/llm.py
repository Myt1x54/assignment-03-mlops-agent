"""LLM abstraction (DECISION D3, D25).

The orchestrator only sees ``LLMClient.generate(system, history, tools) -> LLMReply``.
``GeminiClient`` talks to Google AI Studio via the ``google-genai`` SDK; ``ScriptedLLM`` replays canned
replies so the whole agent can be tested (and run in CI) without any network or API key.

History is provider-neutral:

* ``{"role": "user", "text": str}``
* ``{"role": "assistant", "text": str, "tool_calls": [ToolCall], "raw": <provider content or None>}``
* ``{"role": "tool", "results": [(ToolCall, payload_dict)]}``
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from agent.logging_utils import get_logger

log = get_logger("llm")


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]
    id: str | None = None


@dataclass
class LLMReply:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: Any = None  # provider-native content, replayed verbatim (Gemini thought signatures)


class LLMError(RuntimeError):
    """LLM unavailable after retries, or misconfigured (e.g. missing API key)."""


class LLMClient(Protocol):
    def generate(
        self, system: str, history: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMReply: ...


# --- Gemini ----------------------------------------------------------------------------------------------


class GeminiClient:
    """Gemini via ``google-genai`` with exponential backoff on 429 (free-tier rate limits) and 5xx."""

    def __init__(self, api_key: str, model: str, max_retries: int = 5, base_delay: float = 2.0) -> None:
        if not api_key or api_key == "replace-me":
            raise LLMError("GEMINI_API_KEY is not set (put it in .env; see .env.example)")
        from google import genai
        from google.genai import types

        # Bounded HTTP timeout: a slow/overloaded model must fail (and be retried) rather than hang.
        self._client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=60_000))
        self.model = model
        self.max_retries = max_retries
        self.base_delay = base_delay

    def _contents(self, history: list[dict[str, Any]]) -> list[Any]:
        from google.genai import types

        contents: list[Any] = []
        for msg in history:
            if msg["role"] == "user":
                contents.append(types.Content(role="user", parts=[types.Part.from_text(text=msg["text"])]))
            elif msg["role"] == "assistant":
                if msg.get("raw") is not None:
                    contents.append(msg["raw"])
                    continue
                parts = [types.Part.from_text(text=msg["text"])] if msg.get("text") else []
                parts += [types.Part.from_function_call(name=c.name, args=c.args) for c in msg["tool_calls"]]
                contents.append(types.Content(role="model", parts=parts))
            elif msg["role"] == "tool":
                parts = [
                    types.Part.from_function_response(name=call.name, response=payload)
                    for call, payload in msg["results"]
                ]
                contents.append(types.Content(role="user", parts=parts))
        return contents

    def generate(self, system: str, history: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMReply:
        from google.genai import errors, types

        declarations = [
            types.FunctionDeclaration(
                name=t["name"], description=t["description"], parameters_json_schema=t["parameters"]
            )
            for t in tools
        ]
        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=[types.Tool(function_declarations=declarations)] if declarations else None,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            temperature=0.1,
        )
        contents = self._contents(history)
        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.models.generate_content(
                    model=self.model, contents=contents, config=config
                )
                break
            except errors.APIError as exc:
                retryable = exc.code == 429 or (exc.code or 0) >= 500
                if not retryable or attempt == self.max_retries:
                    raise LLMError(f"Gemini API error {exc.code}: {str(exc)[:300]}") from exc
                delay = self.base_delay * (2**attempt)
                log.warning(
                    "gemini retry", extra={"code": exc.code, "attempt": attempt + 1, "sleep_s": delay}
                )
                time.sleep(delay)
        candidate = (response.candidates or [None])[0]
        if candidate is None or candidate.content is None:
            return LLMReply(text=response.text or "")
        calls = [
            ToolCall(name=p.function_call.name, args=dict(p.function_call.args or {}), id=p.function_call.id)
            for p in candidate.content.parts or []
            if p.function_call
        ]
        text = "".join(
            p.text for p in candidate.content.parts or [] if p.text and not getattr(p, "thought", False)
        )
        return LLMReply(text=text, tool_calls=calls, raw=candidate.content)


# --- scripted fake (tests / CI / offline demo) ----------------------------------------------------------


Step = LLMReply | Callable[[list[dict[str, Any]]], LLMReply]


class ScriptedLLM:
    """Replays scripted replies in order; a step may be a function of the history (reacts to results)."""

    def __init__(self, steps: Sequence[Step]) -> None:
        self.steps = list(steps)
        self.calls: list[dict[str, Any]] = []

    def generate(self, system: str, history: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMReply:
        self.calls.append({"system": system, "history": list(history), "tools": [t["name"] for t in tools]})
        if not self.steps:
            return LLMReply(text="(script exhausted)")
        step = self.steps.pop(0)
        return step(history) if callable(step) else step
