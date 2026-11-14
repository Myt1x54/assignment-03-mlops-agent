"""Agent settings (from environment / ``.env``) and the retraining/safety policy file (DECISION D17).

Secrets (Gemini key, Airflow password) are ``SecretStr`` so they never appear in ``repr`` or logs.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = REPO_ROOT / "config" / "agent_policy.yaml"


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", extra="ignore", env_file_encoding="utf-8")

    gemini_api_key: SecretStr = SecretStr("")
    agent_model: str = "gemini-flash-latest"

    mlflow_tracking_uri: str = "http://localhost:5000"

    airflow_base_url: str = "http://localhost:8080"
    airflow_username: str = "admin"
    airflow_password: SecretStr = SecretStr("")
    airflow_approved_dags: str = "ml_training_pipeline"

    k8s_namespace: str = "mlops-demo"
    k8s_allowed_namespaces: str = "mlops-demo"
    k8s_context: str | None = "kind-mlops"

    agent_tool_timeout_seconds: float = Field(default=20.0, gt=0, le=300)
    agent_max_iterations: int = Field(default=12, ge=1, le=50)
    log_level: str = "INFO"

    repo_root: Path = REPO_ROOT
    policy_path: Path = DEFAULT_POLICY

    @property
    def approved_dags(self) -> list[str]:
        return _csv(self.airflow_approved_dags)

    @property
    def allowed_namespaces(self) -> list[str]:
        return _csv(self.k8s_allowed_namespaces)

    def secret_values(self) -> list[str]:
        """Concrete secret strings to scrub from logs and tool output."""
        values = [self.gemini_api_key.get_secret_value(), self.airflow_password.get_secret_value()]
        return [v for v in values if v and v != "replace-me"]


class RetrainingPolicy(BaseModel):
    max_model_age_days: int = Field(ge=1)
    min_f1: float = Field(ge=0, le=1)
    newer_dataset_triggers: bool = True
    failed_quality_gate_triggers: bool = True


class SafetyPolicy(BaseModel):
    scale_min_replicas: int = Field(ge=0)
    scale_max_replicas: int = Field(ge=1)
    log_tail_max_lines: int = Field(ge=1, le=5000)
    confirmation_ttl_seconds: int = Field(ge=10)


class Policy(BaseModel):
    retraining: RetrainingPolicy
    safety: SafetyPolicy


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def load_policy(path: Path | None = None) -> Policy:
    raw: dict[str, Any] = yaml.safe_load((path or get_settings().policy_path).read_text(encoding="utf-8"))
    return Policy.model_validate(raw)
