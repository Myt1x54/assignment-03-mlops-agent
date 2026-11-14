"""Gemini client wrapper (no network) and CLI transcript rendering."""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest
from google.genai import errors, types

import agent.llm as llm_mod
from agent.llm import GeminiClient, LLMError, ToolCall
from agent.main import transcript_markdown
from agent.orchestrator import Turn
from agent.schemas import AgentResponse, ToolResult, ToolStatus


def gemini(responses) -> GeminiClient:
    client = object.__new__(GeminiClient)
    client.models, client.max_retries, client.base_delay, client.max_sleep = ["gemini-test"], 3, 0.0, 0.0
    queue = list(responses)

    def generate_content(model, contents, config):
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    client._client = NS(models=NS(generate_content=generate_content))
    return client


def response_with(parts) -> NS:
    return NS(candidates=[NS(content=types.Content(role="model", parts=parts))], text=None)


TOOLS = [
    {"name": "mlflow_best_run", "description": "best run", "parameters": {"type": "object", "properties": {}}}
]


def test_missing_api_key_is_a_clear_error():
    with pytest.raises(LLMError, match="GEMINI_API_KEY"):
        GeminiClient("", "gemini-flash-latest")
    with pytest.raises(LLMError):
        GeminiClient("replace-me", "gemini-flash-latest")


def test_function_calls_are_parsed():
    part = types.Part.from_function_call(name="mlflow_best_run", args={"metric": "f1"})
    reply = gemini([response_with([part])]).generate("sys", [{"role": "user", "text": "best?"}], TOOLS)
    assert reply.tool_calls == [ToolCall(name="mlflow_best_run", args={"metric": "f1"}, id=None)]
    assert reply.raw is not None  # replayed verbatim on the next turn


def test_rate_limit_is_retried_with_backoff(monkeypatch):
    sleeps = []
    monkeypatch.setattr(llm_mod.time, "sleep", sleeps.append)
    quota = errors.APIError(429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}})
    ok = response_with([types.Part.from_text(text="answer")])
    reply = gemini([quota, quota, ok]).generate("sys", [{"role": "user", "text": "hi"}], TOOLS)
    assert reply.text == "answer"
    assert len(sleeps) == 2


def test_non_retryable_error_raises_llm_error():
    bad = errors.APIError(
        400, {"error": {"code": 400, "message": "bad request", "status": "INVALID_ARGUMENT"}}
    )
    with pytest.raises(LLMError, match="400"):
        gemini([bad]).generate("sys", [{"role": "user", "text": "hi"}], TOOLS)


def test_history_conversion_includes_tool_responses():
    client = gemini([])
    call = ToolCall("mlflow_best_run", {})
    contents = client._contents(
        [
            {"role": "user", "text": "best?"},
            {"role": "assistant", "text": "", "tool_calls": [call], "raw": None},
            {"role": "tool", "results": [(call, {"status": "ok", "data": {"f1": 0.81}})]},
        ]
    )
    assert [c.role for c in contents] == ["user", "model", "user"]
    assert contents[2].parts[0].function_response.name == "mlflow_best_run"
    assert contents[1].parts[0].thought_signature == llm_mod.SKIP_SIGNATURE  # foreign history is accepted


def test_transcript_lists_tool_calls_and_redacts_secrets():
    result = ToolResult(tool="airflow_list_dags", request_id="r", status=ToolStatus.OK, evidence_id="E1")
    turn = Turn(
        question="list dags",
        response=AgentResponse(
            request_id="req-1", answer="One DAG [E1]. token=abc123secretvalue", tool_calls=1
        ),
        results=[result],
        calls=[ToolCall("airflow_list_dags", {"limit": 5})],
        confirmations=[{"tool": "k8s_restart_deployment", "summary": "restart churn-api", "approved": False}],
    )
    md = transcript_markdown([turn], "gemini-test")
    assert '**E1** `airflow_list_dags({"limit": 5})` -> `ok`' in md
    assert "declined" in md
    assert "abc123secretvalue" not in md


def test_daily_quota_switches_to_fallback_model(monkeypatch):
    monkeypatch.setattr(llm_mod.time, "sleep", lambda s: None)
    daily = errors.APIError(
        429,
        {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED", "details": [
            {"violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]},
            {"retryDelay": "77691s"},
        ]}},
    )  # fmt: skip
    seen = []
    client = gemini([])
    client.models = ["gemini-main", "gemini-lite"]
    answers = [daily, response_with([types.Part.from_text(text="from fallback")])]

    def generate_content(model, contents, config):
        seen.append(model)
        item = answers.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    client._client = NS(models=NS(generate_content=generate_content))
    reply = client.generate("sys", [{"role": "user", "text": "hi"}], TOOLS)
    assert reply.text == "from fallback"
    assert seen == ["gemini-main", "gemini-lite"]
    assert client.model == "gemini-lite"


def test_per_minute_limit_uses_server_retry_delay(monkeypatch):
    sleeps = []
    monkeypatch.setattr(llm_mod.time, "sleep", sleeps.append)
    minute = errors.APIError(
        429,
        {
            "error": {
                "code": 429,
                "message": "rate",
                "status": "RESOURCE_EXHAUSTED",
                "details": [{"retryDelay": "7s"}],
            }
        },
    )
    client = gemini([minute, response_with([types.Part.from_text(text="ok")])])
    client.max_sleep = 65.0
    assert client.generate("sys", [{"role": "user", "text": "hi"}], TOOLS).text == "ok"
    assert sleeps == [7.0]
