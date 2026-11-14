"""Export a registered model version from MLflow into ``model/`` for the inference image build.

Writes ``model/model.joblib`` (plain sklearn Pipeline, so the image does not need MLflow) and
``model/meta.json`` (model version, alias, run id, dataset version) which ``/version`` reports.

Usage::

    uv run python scripts/export_model.py --alias champion
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import joblib
import mlflow
import mlflow.sklearn
from mlflow import MlflowClient


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="churn-classifier")
    parser.add_argument("--alias", default="champion")
    parser.add_argument("--out", type=Path, default=Path("model"))
    args = parser.parse_args()

    mlflow.set_tracking_uri(os.environ.get("MLFLOW_TRACKING_URI", "http://localhost:5000"))
    client = MlflowClient()
    mv = client.get_model_version_by_alias(args.model, args.alias)
    run = client.get_run(mv.run_id)
    model = mlflow.sklearn.load_model(f"models:/{args.model}@{args.alias}")

    args.out.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, args.out / "model.joblib")
    meta = {
        "registered_model": args.model,
        "model_version": mv.version,
        "alias": args.alias,
        "run_id": mv.run_id,
        "data_version": run.data.tags.get("data.version"),
        "data_dvc_md5": run.data.tags.get("data.dvc_md5"),
        "f1": run.data.metrics.get("f1"),
    }
    (args.out / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"exported {args.model} v{mv.version} (@{args.alias}) -> {args.out}")


if __name__ == "__main__":
    main()
