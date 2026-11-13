"""Quality gate + promotion (DECISION D10).

``check`` compares the evaluation metrics against thresholds in ``params.yaml``. Only when the gate passes
may ``promote`` move the registry alias ``champion`` to the new model version. A failed gate tags the
run/version as ``quality_gate=failed`` so the agent can later explain *why* a model was not promoted.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from training.params import load_params


def check(metrics: dict[str, float], gate: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if metrics.get("f1", 0.0) < gate["min_f1"]:
        reasons.append(f"f1 {metrics.get('f1')} < min_f1 {gate['min_f1']}")
    if metrics.get("roc_auc", 0.0) < gate["min_roc_auc"]:
        reasons.append(f"roc_auc {metrics.get('roc_auc')} < min_roc_auc {gate['min_roc_auc']}")
    return (not reasons), reasons


def record_gate(result: dict[str, Any], passed: bool, reasons: list[str]) -> None:
    """Tag the MLflow run and model version with the gate outcome (no-op without MLflow info)."""
    if "run_id" not in result:
        return
    from mlflow import MlflowClient

    client = MlflowClient(tracking_uri=os.environ.get("MLFLOW_TRACKING_URI", "sqlite:///mlflow.db"))
    status = "passed" if passed else "failed"
    client.set_tag(result["run_id"], "quality_gate", status)
    client.set_tag(result["run_id"], "quality_gate.reasons", "; ".join(reasons) or "none")
    client.set_model_version_tag(result["registered_model"], result["model_version"], "quality_gate", status)


def promote(result: dict[str, Any]) -> str:
    from mlflow import MlflowClient

    client = MlflowClient(tracking_uri=os.environ.get("MLFLOW_TRACKING_URI", "sqlite:///mlflow.db"))
    client.set_registered_model_alias(result["registered_model"], "champion", result["model_version"])
    client.set_tag(result["run_id"], "promoted", "true")
    return result["model_version"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply the quality gate, or promote a model that passed it")
    parser.add_argument("--metrics", type=Path, default=Path("reports/metrics.json"))
    parser.add_argument(
        "--action",
        choices=["check", "promote"],
        default="check",
        help="check: evaluate + record the gate; promote: re-verify the gate, then set alias 'champion'",
    )
    args = parser.parse_args(argv)

    result = json.loads(args.metrics.read_text(encoding="utf-8"))
    passed, reasons = check(result["metrics"], load_params()["quality_gate"])

    if args.action == "check":
        record_gate(result, passed, reasons)
        print("quality gate PASSED" if passed else f"quality gate FAILED: {reasons}")
        return 0 if passed else 1

    # promote: defense in depth — never promote a model that does not pass the gate.
    if not passed:
        print(f"refusing to promote: quality gate not passed ({reasons})")
        return 1
    if "run_id" not in result:
        print("refusing to promote: metrics were not logged to MLflow")
        return 1
    print(f"promoted model version {promote(result)} to alias 'champion'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
