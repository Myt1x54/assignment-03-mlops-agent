"""PLAN S14 required tests 10-11: quality gate, data validation, FastAPI readiness and prediction."""

from __future__ import annotations

import json
from pathlib import Path

import joblib
import pytest
from fastapi.testclient import TestClient

import api.main as api
from training import quality_gate
from training.make_dataset import generate
from training.params import load_params
from training.preprocess import FEATURES, TARGET
from training.train import build_pipeline
from training.validate import validate_frame

GATE = load_params()["quality_gate"]


# 10 - quality gate -----------------------------------------------------------------------------------------
def test_quality_gate_passes_good_model():
    passed, reasons = quality_gate.check({"f1": 0.81, "roc_auc": 0.87}, GATE)
    assert passed and reasons == []


def test_quality_gate_blocks_weak_model_with_reasons():
    passed, reasons = quality_gate.check({"f1": 0.72, "roc_auc": 0.64}, GATE)
    assert not passed
    assert any("roc_auc" in r for r in reasons)


def test_promote_refuses_model_that_fails_gate(tmp_path, monkeypatch):
    metrics = tmp_path / "metrics.json"
    metrics.write_text(
        json.dumps({"metrics": {"f1": 0.5, "roc_auc": 0.6}, "run_id": "x" * 32}), encoding="utf-8"
    )
    called = []
    monkeypatch.setattr(quality_gate, "promote", lambda *a, **k: called.append(a) or (True, "promoted"))
    assert quality_gate.main(["--metrics", str(metrics), "--action", "promote"]) == 1
    assert called == []  # never reached the registry


@pytest.mark.parametrize(
    ("candidate", "champion", "expected"),
    [(0.82, 0.81, True), (0.80, 0.81, False), (0.81, 0.81, False), (0.70, None, True)],
)
def test_champion_challenger_rule(candidate, champion, expected):
    assert quality_gate.is_better(candidate, champion) is expected


# 10 - data validation --------------------------------------------------------------------------------------
def test_validation_accepts_clean_dataset():
    report = validate_frame(generate("v1"))
    assert report.passed, report.errors


def test_validation_catches_corrupt_batch():
    report = validate_frame(generate("corrupt"))
    assert not report.passed
    text = " ".join(report.errors)
    assert "null" in text.lower()
    assert "contract_type" in text


def test_validation_catches_missing_columns():
    report = validate_frame(generate("v1").drop(columns=["tenure_months"]))
    assert not report.passed
    assert "missing columns" in report.errors[0]


def test_validation_catches_too_few_rows():
    report = validate_frame(generate("v1").head(10))
    assert not report.passed
    assert any("rows" in e for e in report.errors)


# 11 - FastAPI ----------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def model_dir(tmp_path_factory) -> Path:
    df = generate("v1").head(1500)
    model = build_pipeline("logistic_regression", {"max_iter": 300}).fit(df[FEATURES], df[TARGET])
    out = tmp_path_factory.mktemp("model")
    joblib.dump(model, out / "model.joblib")
    (out / "meta.json").write_text(json.dumps({"model_version": "7", "alias": "champion"}), encoding="utf-8")
    return out


CUSTOMER = {
    "tenure_months": 2,
    "monthly_charges": 95.5,
    "total_charges": 191.0,
    "support_calls": 5,
    "paperless_billing": 1,
    "senior_citizen": 0,
    "contract_type": "month-to-month",
    "internet_service": "fiber",
    "payment_method": "electronic-check",
}


def client_for(monkeypatch, model_path: Path, meta_path: Path) -> TestClient:
    monkeypatch.setattr(api.settings, "model_path", model_path)
    monkeypatch.setattr(api.settings, "meta_path", meta_path)
    return TestClient(api.app)


def test_api_ready_and_predicts(monkeypatch, model_dir):
    with client_for(monkeypatch, model_dir / "model.joblib", model_dir / "meta.json") as client:
        assert client.get("/healthz").json() == {"status": "alive"}
        ready = client.get("/readyz")
        assert ready.status_code == 200
        assert ready.json()["model_version"] == "7"
        resp = client.post("/predict", json={"customers": [CUSTOMER]})
        assert resp.status_code == 200
        pred = resp.json()["predictions"][0]
        assert 0.0 <= pred["churn_probability"] <= 1.0
        assert client.get("/version").json()["model_alias"] == "champion"


def test_api_not_ready_when_model_path_is_wrong(monkeypatch, tmp_path):
    """Failure F1 (broken ConfigMap MODEL_PATH): liveness OK, readiness 503 with the reason."""
    with client_for(monkeypatch, tmp_path / "wrong.joblib", tmp_path / "meta.json") as client:
        assert client.get("/healthz").status_code == 200
        ready = client.get("/readyz")
        assert ready.status_code == 503
        assert "model file not found" in ready.json()["reason"]
        assert client.post("/predict", json={"customers": [CUSTOMER]}).status_code == 503


def test_api_rejects_invalid_payload(monkeypatch, model_dir):
    with client_for(monkeypatch, model_dir / "model.joblib", model_dir / "meta.json") as client:
        bad = {**CUSTOMER, "contract_type": "lifetime", "tenure_months": -3}
        assert client.post("/predict", json={"customers": [bad]}).status_code == 422
        assert client.post("/predict", json={"customers": []}).status_code == 422
