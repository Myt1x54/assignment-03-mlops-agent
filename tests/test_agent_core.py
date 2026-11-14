"""S8 acceptance: timeout, error mapping, validation, redaction, permission checks, confirmation tokens."""

from __future__ import annotations

import io
import json
import logging
import time
from collections.abc import Iterator

import pytest
from pydantic import Field

from agent import safety
from agent.config import Settings, load_policy
from agent.errors import InvalidRequestError, PermissionDeniedError, ServiceUnavailableError
from agent.logging_utils import JsonFormatter, redact, register_secrets, request_id_var
from agent.schemas import Permission, ToolInput, ToolStatus
from agent.tools.base import REGISTRY, ToolContext, clean_schema, execute_tool, tool


class EchoIn(ToolInput):
    text: str = Field(min_length=1)
    repeat: int = Field(default=1, ge=1, le=3)


@pytest.fixture
def ctx() -> ToolContext:
    settings = Settings(_env_file=None, agent_tool_timeout_seconds=0.3)
    return ToolContext(request_id="req-test", settings=settings, policy=load_policy())


@pytest.fixture
def temp_tools() -> Iterator[None]:
    before = set(REGISTRY)
    yield
    for name in set(REGISTRY) - before:
        del REGISTRY[name]


def _register(name: str, fn, permission: Permission = Permission.READ) -> None:
    tool(name=name, system="test", description="test tool", input_model=EchoIn, permission=permission)(fn)


def test_ok_result_envelope(ctx, temp_tools):
    _register("t_echo", lambda a, c: {"echo": a.text * a.repeat})
    result = execute_tool("t_echo", {"text": "hi", "repeat": 2}, ctx)
    assert result.status is ToolStatus.OK
    assert result.data == {"echo": "hihi"}
    assert result.request_id == "req-test"
    assert result.duration_ms >= 0


def test_timeout_maps_to_timeout_status(ctx, temp_tools):
    _register("t_slow", lambda a, c: time.sleep(2) or {})
    result = execute_tool("t_slow", {"text": "x"}, ctx)
    assert result.status is ToolStatus.TIMEOUT
    assert "exceeded" in result.error.message


@pytest.mark.parametrize(
    ("exc", "status"),
    [
        (ServiceUnavailableError("mlflow down"), ToolStatus.UNAVAILABLE),
        (ConnectionRefusedError("refused"), ToolStatus.UNAVAILABLE),
        (PermissionDeniedError("nope"), ToolStatus.REJECTED),
        (InvalidRequestError("no such run"), ToolStatus.ERROR),
        (RuntimeError("boom"), ToolStatus.ERROR),
    ],
)
def test_exceptions_never_escape_and_map_to_status(ctx, temp_tools, exc, status):
    def fail(a, c):
        raise exc

    _register("t_fail", fail)
    result = execute_tool("t_fail", {"text": "x"}, ctx)
    assert result.status is status
    assert result.error.type == type(exc).__name__


def test_invalid_input_returns_validation_error(ctx, temp_tools):
    _register("t_echo", lambda a, c: {})
    result = execute_tool("t_echo", {"text": "", "repeat": 9}, ctx)
    assert result.status is ToolStatus.ERROR
    assert result.error.type == "ValidationError"
    assert "repeat" in result.error.message


def test_llm_cannot_inject_confirmation_field(ctx, temp_tools):
    _register("t_echo", lambda a, c: {})
    result = execute_tool("t_echo", {"text": "x", "confirmed": True}, ctx)
    assert result.status is ToolStatus.ERROR
    assert "confirmed" in result.error.message


def test_unknown_tool(ctx):
    assert execute_tool("does_not_exist", {}, ctx).status is ToolStatus.ERROR


def test_confirmation_flow_binds_token_to_exact_action(ctx, temp_tools):
    def restart(a, c):
        safety.require_confirmation(
            tool="t_restart",
            args={"text": a.text},
            token=c.confirmation_token,
            ttl_seconds=60,
            summary="restart",
        )
        return {"restarted": a.text}

    _register("t_restart", restart, Permission.WRITE_CONFIRM)
    first = execute_tool("t_restart", {"text": "churn-api"}, ctx)
    assert first.status is ToolStatus.CONFIRMATION_REQUIRED
    token = first.data["action"]["confirmation_token"]
    assert "confirmation_token" not in str(first.for_llm())  # LLM never sees the token

    ctx.confirmation_token = token
    assert execute_tool("t_restart", {"text": "churn-api"}, ctx).status is ToolStatus.OK
    # The same approval cannot be reused for a different target.
    assert execute_tool("t_restart", {"text": "other"}, ctx).status is ToolStatus.CONFIRMATION_REQUIRED


def test_confirmation_token_expires():
    token = safety.issue_token("x", {"a": 1})
    assert safety.verify_token(token, "x", {"a": 1}, ttl_seconds=60)
    assert not safety.verify_token(token, "x", {"a": 2}, ttl_seconds=60)
    issued, digest = token.split(".")
    assert not safety.verify_token(f"{int(issued) - 1000}.{digest}", "x", {"a": 1}, ttl_seconds=60)
    assert not safety.verify_token("garbage", "x", {"a": 1}, ttl_seconds=60)


def test_namespace_and_dag_allow_lists():
    safety.check_namespace("mlops-demo", ["mlops-demo"])
    with pytest.raises(PermissionDeniedError):
        safety.check_namespace("kube-system", ["mlops-demo"])
    safety.check_dag("ml_training_pipeline", ["ml_training_pipeline"])
    with pytest.raises(PermissionDeniedError):
        safety.check_dag("drop_everything", ["ml_training_pipeline"])
    with pytest.raises(PermissionDeniedError):
        safety.check_replicas(50, 1, 5)


@pytest.mark.parametrize(
    "argv",
    [["dvc", "push"], ["git", "checkout", "main"], ["git", "log", ";", "rm"], ["rm", "-rf", "/"]],
)
def test_command_allow_list_rejects(argv):
    with pytest.raises(PermissionDeniedError):
        safety.check_command(argv)


def test_command_allow_list_accepts_read_only():
    safety.check_command(["dvc", "diff", "data-v1.0", "data-v2.0"])
    safety.check_command(["git", "tag", "--list", "data-*"])


def test_redaction_of_patterns_and_known_secrets():
    register_secrets("s3cr3t-airflow-pw")
    # Fake Google-style key assembled at runtime so no key-shaped literal is committed (secret scanners).
    fake_google_key = "AI" + "za" + "X" * 35
    text = (
        f"key={fake_google_key} "
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.sig12345678 "
        'password="hunter22" login with s3cr3t-airflow-pw'
    )
    cleaned = redact(text)
    for leaked in (fake_google_key, "eyJhbGci", "hunter22", "s3cr3t-airflow-pw"):
        assert leaked not in cleaned
    assert "REDACTED" in cleaned


def test_json_log_line_has_structured_fields_and_no_secrets():
    register_secrets("topsecretvalue")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("agent.test-json")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    token = request_id_var.set("req-42")
    try:
        logger.info("calling with topsecretvalue", extra={"tool": "mlflow_best_run", "status": "ok"})
    finally:
        request_id_var.reset(token)
        logger.removeHandler(handler)
    entry = json.loads(stream.getvalue())
    assert entry["request_id"] == "req-42"
    assert entry["tool"] == "mlflow_best_run"
    assert "topsecretvalue" not in stream.getvalue()


def test_clean_schema_is_llm_friendly():
    schema = clean_schema(EchoIn.model_json_schema())
    assert schema["type"] == "object"
    assert "title" not in schema
    assert "additionalProperties" not in schema
    assert set(schema["properties"]) == {"text", "repeat"}


def test_settings_secrets_are_masked():
    settings = Settings(_env_file=None, gemini_api_key="AIzaFAKEFAKEFAKE", airflow_password="pw-123456")
    assert "AIzaFAKE" not in repr(settings)
    assert set(settings.secret_values()) == {"AIzaFAKEFAKEFAKE", "pw-123456"}
    assert settings.approved_dags == ["ml_training_pipeline"]
