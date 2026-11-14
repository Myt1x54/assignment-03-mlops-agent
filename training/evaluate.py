"""Evaluation stage: compute test metrics and log everything to MLflow with dataset provenance.

Creates one MLflow run per training, containing:
- params (model type, hyperparameters, split settings),
- metrics (accuracy, precision, recall, f1, roc_auc),
- tags (dataset provenance from ``training.provenance`` + pipeline run id),
- artifacts (confusion matrix, validation report, train info) and the model,
- a new version of the registered model with alias ``candidate``.

Writes ``reports/metrics.json`` (consumed by the quality gate and by DVC as a metrics file).
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import joblib
import pandas as pd
from sklearn.metrics import (
    accuracy_score,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)

from training import provenance
from training.params import load_params
from training.preprocess import FEATURES, TARGET

DEFAULT_TRACKING_URI = "sqlite:///mlflow.db"


def compute_metrics(model: Any, test: pd.DataFrame) -> tuple[dict[str, float], list[list[int]]]:
    y_true = test[TARGET]
    y_pred = model.predict(test[FEATURES])
    y_prob = model.predict_proba(test[FEATURES])[:, 1]
    metrics = {
        "accuracy": accuracy_score(y_true, y_pred),
        "precision": precision_score(y_true, y_pred, zero_division=0),
        "recall": recall_score(y_true, y_pred, zero_division=0),
        "f1": f1_score(y_true, y_pred, zero_division=0),
        "roc_auc": roc_auc_score(y_true, y_prob),
    }
    return {k: round(float(v), 4) for k, v in metrics.items()}, confusion_matrix(y_true, y_pred).tolist()


def trusted_types(model: Any) -> list[str]:
    """Types skops flags as untrusted in *our own* freshly trained pipeline (DECISION D29).

    MLflow 3 stores sklearn models with skops (no arbitrary-code pickle). Some estimators (e.g. tree
    internals) are not on skops' default allow-list; since this model was built in this very process
    from scikit-learn classes, those types are trusted explicitly instead of falling back to pickle.
    """
    import skops.io

    return skops.io.get_untrusted_types(data=skops.io.dumps(model))


def log_to_mlflow(
    model: Any,
    metrics: dict[str, float],
    cm: list[list[int]],
    train_info: dict[str, Any],
    params: dict[str, Any],
    tags: dict[str, str],
    test: pd.DataFrame,
    reports_dir: Path,
) -> dict[str, str]:
    import mlflow
    import mlflow.sklearn
    from mlflow import MlflowClient

    mlflow.set_tracking_uri(os.environ.get("MLFLOW_TRACKING_URI", DEFAULT_TRACKING_URI))
    mlflow.set_experiment(os.environ.get("MLFLOW_EXPERIMENT", params["mlflow"]["experiment"]))
    registered = params["mlflow"]["registered_model"]

    run_name = f"{train_info['model_type']}-{tags['data.version']}"
    with mlflow.start_run(run_name=run_name) as run:
        mlflow.log_params(
            {
                "model_type": train_info["model_type"],
                "train_rows": train_info["train_rows"],
                "test_rows": len(test),
                "split_seed": params["split"]["seed"],
                "test_size": params["split"]["test_size"],
                **{f"hp.{k}": v for k, v in train_info["hyperparams"].items()},
            }
        )
        mlflow.log_metrics(metrics)
        mlflow.log_metric("train_seconds", train_info["train_seconds"])
        mlflow.set_tags(tags)
        mlflow.log_dict({"labels": [0, 1], "matrix": cm}, "evaluation/confusion_matrix.json")
        mlflow.log_dict(train_info, "evaluation/train_info.json")
        validation = reports_dir / "validation.json"
        if validation.exists():
            mlflow.log_artifact(str(validation), "evaluation")
        info = mlflow.sklearn.log_model(
            model,
            name="model",
            skops_trusted_types=trusted_types(model),
            registered_model_name=registered,
            input_example=test[FEATURES].head(3),
        )
        version = str(info.registered_model_version)
        run_id = run.info.run_id

    client = MlflowClient()
    client.set_registered_model_alias(registered, "candidate", version)
    client.set_model_version_tag(registered, version, "data.version", tags["data.version"])
    client.set_model_version_tag(registered, version, "quality_gate", "pending")
    return {"run_id": run_id, "model_version": version, "registered_model": registered}


def run(
    test_path: Path,
    model_dir: Path,
    reports_dir: Path,
    pipeline_run_id: str | None = None,
    use_mlflow: bool = True,
) -> dict[str, Any]:
    params = load_params()
    model = joblib.load(model_dir / "model.joblib")
    train_info = json.loads((model_dir / "train_info.json").read_text(encoding="utf-8"))
    test = pd.read_csv(test_path)
    metrics, cm = compute_metrics(model, test)

    tags = provenance.collect()
    tags["pipeline.run_id"] = pipeline_run_id or os.environ.get("PIPELINE_RUN_ID", "manual")
    tags["model.type"] = train_info["model_type"]

    result: dict[str, Any] = {"metrics": metrics, "confusion_matrix": cm, "provenance": tags}
    if use_mlflow:
        result.update(log_to_mlflow(model, metrics, cm, train_info, params, tags, test, reports_dir))

    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "metrics.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    print(
        f"evaluate: {metrics}  run_id={result.get('run_id', '-')}  version={result.get('model_version', '-')}"
    )
    return result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Evaluate the model and log to MLflow")
    parser.add_argument("--test", type=Path, default=Path("data/processed/test.csv"))
    parser.add_argument("--model-dir", type=Path, default=Path("models"))
    parser.add_argument("--reports-dir", type=Path, default=Path("reports"))
    parser.add_argument("--pipeline-run-id", default=None)
    parser.add_argument("--no-mlflow", action="store_true", help="compute metrics only (offline)")
    args = parser.parse_args(argv)
    run(args.test, args.model_dir, args.reports_dir, args.pipeline_run_id, use_mlflow=not args.no_mlflow)


if __name__ == "__main__":
    main()
