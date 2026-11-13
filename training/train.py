"""Training stage: fit an sklearn Pipeline (encoding + classifier) on the training split.

Writes ``models/model.joblib`` and ``models/train_info.json``. Evaluation and MLflow logging happen in
``training.evaluate`` so the Airflow DAG can show them as separate tasks.

Usage::

    uv run python -m training.train [--model-type random_forest]
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import joblib
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from training.params import load_params
from training.preprocess import BINARY_FEATURES, CATEGORICAL_FEATURES, FEATURES, NUMERIC_FEATURES, TARGET

MODEL_TYPES = {
    "logistic_regression": LogisticRegression,
    "random_forest": RandomForestClassifier,
    "gradient_boosting": GradientBoostingClassifier,
}


def build_pipeline(model_type: str, hyperparams: dict[str, Any]) -> Pipeline:
    if model_type not in MODEL_TYPES:
        raise ValueError(f"unknown model type {model_type!r}; choose from {list(MODEL_TYPES)}")
    encoder = ColumnTransformer(
        [
            ("num", StandardScaler(), NUMERIC_FEATURES),
            ("bin", "passthrough", BINARY_FEATURES),
            ("cat", OneHotEncoder(handle_unknown="ignore"), CATEGORICAL_FEATURES),
        ]
    )
    return Pipeline([("encode", encoder), ("clf", MODEL_TYPES[model_type](**hyperparams))])


def run(train_path: Path, model_dir: Path, model_type: str, hyperparams: dict[str, Any]) -> dict[str, Any]:
    df = pd.read_csv(train_path)
    pipeline = build_pipeline(model_type, hyperparams)
    started = time.perf_counter()
    pipeline.fit(df[FEATURES], df[TARGET])
    duration = round(time.perf_counter() - started, 3)

    model_dir.mkdir(parents=True, exist_ok=True)
    joblib.dump(pipeline, model_dir / "model.joblib")
    info = {
        "model_type": model_type,
        "hyperparams": hyperparams,
        "train_rows": len(df),
        "train_seconds": duration,
        "features": FEATURES,
    }
    (model_dir / "train_info.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    print(f"train: {model_type} fitted on {len(df)} rows in {duration}s -> {model_dir / 'model.joblib'}")
    return info


def main(argv: list[str] | None = None) -> None:
    params = load_params()
    parser = argparse.ArgumentParser(description="Train the churn classifier")
    parser.add_argument("--train", type=Path, default=Path("data/processed/train.csv"))
    parser.add_argument("--model-dir", type=Path, default=Path("models"))
    parser.add_argument("--model-type", default=params["model"]["type"], choices=list(MODEL_TYPES))
    args = parser.parse_args(argv)
    run(args.train, args.model_dir, args.model_type, params["model"].get(args.model_type, {}))


if __name__ == "__main__":
    main()
