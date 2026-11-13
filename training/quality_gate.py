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


def is_better(candidate_f1: float, champion_f1: float | None) -> bool:
    """Champion/challenger rule (DECISION D32): promote only on a strict F1 improvement."""
    return champion_f1 is None or candidate_f1 > champion_f1


def current_champion_f1(client: Any, registered_model: str) -> tuple[str | None, float | None]:
    try:
        mv = client.get_model_version_by_alias(registered_model, "champion")
    except Exception:  # no champion yet
        return None, None
    return mv.version, client.get_run(mv.run_id).data.metrics.get("f1")


def promote(result: dict[str, Any], force: bool = False) -> tuple[bool, str]:
    """Move alias ``champion`` here if this version beats the current champion; returns (promoted, msg)."""
    from mlflow import MlflowClient

    client = MlflowClient(tracking_uri=os.environ.get("MLFLOW_TRACKING_URI", "sqlite:///mlflow.db"))
    registered, version = result["registered_model"], result["model_version"]
    champ_version, champ_f1 = current_champion_f1(client, registered)
    f1 = result["metrics"]["f1"]
    if not force and not is_better(f1, champ_f1):
        msg = f"kept champion v{champ_version} (f1={champ_f1}); candidate v{version} f1={f1} is not better"
        client.set_tag(result["run_id"], "promoted", "false")
        client.set_tag(result["run_id"], "promotion.reason", msg)
        client.set_model_version_tag(registered, version, "promotion", "skipped: not better than champion")
        return False, msg
    client.set_registered_model_alias(registered, "champion", version)
    client.set_tag(result["run_id"], "promoted", "true")
    client.set_tag(result["run_id"], "promotion.reason", f"f1 {f1} > previous champion f1 {champ_f1}")
    client.set_model_version_tag(registered, version, "promotion", "promoted")
    return (
        True,
        f"promoted v{version} (f1={f1}) to alias 'champion' (previous: v{champ_version}, f1={champ_f1})",
    )


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
    _, message = promote(result)
    print(message)
    return 0  # not promoting a non-improving model is a normal outcome, not a task failure


if __name__ == "__main__":
    sys.exit(main())
