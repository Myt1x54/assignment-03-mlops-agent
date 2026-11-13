"""FastAPI inference service for the churn classifier (deployed on Kubernetes, DECISION D24).

Endpoints
---------
GET  /healthz  liveness  — process is up (does not depend on the model)
GET  /readyz   readiness — 200 only when the model is loaded; 503 with the reason otherwise
GET  /version  app version, model version, MLflow run id and dataset version
POST /predict  churn probability + label for one or more customers

Configuration comes from environment variables (populated from the Kubernetes ConfigMap):
MODEL_PATH, MODEL_META_PATH, APP_VERSION, PREDICTION_THRESHOLD, LOG_LEVEL.
"""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import joblib
import pandas as pd
from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, Field

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
)
log = logging.getLogger("churn-api")


class Settings:
    def __init__(self) -> None:
        self.model_path = Path(os.environ.get("MODEL_PATH", "model/model.joblib"))
        self.meta_path = Path(os.environ.get("MODEL_META_PATH", "model/meta.json"))
        self.app_version = os.environ.get("APP_VERSION", "dev")
        self.threshold = float(os.environ.get("PREDICTION_THRESHOLD", "0.5"))


class ModelState:
    def __init__(self) -> None:
        self.model: Any = None
        self.meta: dict[str, Any] = {}
        self.error: str | None = None
        self.loaded_at: float | None = None

    def load(self, settings: Settings) -> None:
        try:
            if not settings.model_path.exists():
                raise FileNotFoundError(f"model file not found at {settings.model_path}")
            self.model = joblib.load(settings.model_path)
            if settings.meta_path.exists():
                self.meta = json.loads(settings.meta_path.read_text(encoding="utf-8"))
            self.loaded_at = time.time()
            self.error = None
            log.info("model loaded from %s (version=%s)", settings.model_path, self.meta.get("model_version"))
        except Exception as exc:  # readiness must report any load failure
            self.model = None
            self.error = f"{type(exc).__name__}: {exc}"
            log.error("model load failed: %s", self.error)


settings = Settings()
state = ModelState()


@asynccontextmanager
async def lifespan(_: FastAPI):  # type: ignore[no-untyped-def]
    state.load(settings)
    yield


app = FastAPI(title="Churn prediction API", version=settings.app_version, lifespan=lifespan)


class Customer(BaseModel):
    tenure_months: int = Field(ge=0, le=72)
    monthly_charges: float = Field(ge=0, le=200)
    total_charges: float = Field(ge=0)
    support_calls: int = Field(ge=0, le=20)
    paperless_billing: Literal[0, 1]
    senior_citizen: Literal[0, 1]
    contract_type: Literal["month-to-month", "one-year", "two-year"]
    internet_service: Literal["dsl", "fiber", "none"]
    payment_method: Literal["electronic-check", "mailed-check", "bank-transfer", "credit-card"]


class PredictRequest(BaseModel):
    customers: list[Customer] = Field(min_length=1, max_length=1000)


class Prediction(BaseModel):
    churn_probability: float
    churn: bool


class PredictResponse(BaseModel):
    predictions: list[Prediction]
    model_version: str | None
    app_version: str


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "alive"}


@app.get("/readyz")
def readyz(response: Response) -> dict[str, Any]:
    if state.model is None:
        response.status_code = 503
        return {"status": "not-ready", "reason": state.error or "model not loaded"}
    return {"status": "ready", "model_version": state.meta.get("model_version")}


@app.get("/version")
def version() -> dict[str, Any]:
    return {
        "app_version": settings.app_version,
        "model_version": state.meta.get("model_version"),
        "model_alias": state.meta.get("alias"),
        "mlflow_run_id": state.meta.get("run_id"),
        "data_version": state.meta.get("data_version"),
        "model_loaded": state.model is not None,
    }


@app.post("/predict", response_model=PredictResponse)
def predict(request: PredictRequest) -> PredictResponse:
    if state.model is None:
        raise HTTPException(status_code=503, detail=f"model not loaded: {state.error}")
    frame = pd.DataFrame([c.model_dump() for c in request.customers])
    probs = state.model.predict_proba(frame)[:, 1]
    preds = [
        Prediction(churn_probability=round(float(p), 4), churn=bool(p >= settings.threshold)) for p in probs
    ]
    log.info("predict n=%d", len(preds))
    return PredictResponse(
        predictions=preds, model_version=state.meta.get("model_version"), app_version=settings.app_version
    )
