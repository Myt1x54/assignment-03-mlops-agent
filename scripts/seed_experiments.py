"""Seed the MLflow server with a realistic experiment history (PLAN S4, DECISION D8).

For each DVC dataset version (git tags ``data-v1.0`` and ``data-v2.0``) the script really checks the data out
with DVC, then trains and evaluates several model configurations through the normal ``training`` modules.
Every run therefore carries genuine provenance tags (dataset version, DVC md5, git commit).

Experiments created:

* ``churn-training``: one run per model type and dataset version (baseline comparison).
* ``churn-hyperparam-search``: hyperparameter variations on v2, including a deliberately weak model that
  fails the quality gate (so the agent can explain why a model was *not* promoted).

Afterwards the best gate-passing v2 model is promoted to alias ``champion``. The working tree is restored to
the checked-out dataset version at the end.

Usage::

    $env:MLFLOW_TRACKING_URI = "http://localhost:5000"
    uv run python scripts/seed_experiments.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from training import evaluate, preprocess, quality_gate, train, validate  # noqa: E402
from training.params import load_params  # noqa: E402

POINTER = "data/raw/churn.csv.dvc"
DATA = ROOT / "data" / "raw" / "churn.csv"

BASELINE = "churn-training"
SEARCH = "churn-hyperparam-search"

PLAN: list[tuple[str, str, str, dict[str, Any]]] = [
    # (dataset tag, experiment, model type, hyperparameter overrides)
    ("data-v1.0", BASELINE, "logistic_regression", {}),
    ("data-v1.0", BASELINE, "random_forest", {}),
    ("data-v1.0", BASELINE, "gradient_boosting", {}),
    ("data-v2.0", BASELINE, "logistic_regression", {}),
    ("data-v2.0", BASELINE, "random_forest", {}),
    ("data-v2.0", BASELINE, "gradient_boosting", {}),
    ("data-v2.0", SEARCH, "random_forest", {"n_estimators": 400, "max_depth": 12}),
    ("data-v2.0", SEARCH, "gradient_boosting", {"n_estimators": 300, "learning_rate": 0.1}),
    ("data-v2.0", SEARCH, "logistic_regression", {"C": 0.1}),
    # Deliberately under-fitted: a single shallow tree stump, unbalanced -> expected to fail the gate.
    ("data-v2.0", SEARCH, "random_forest", {"n_estimators": 1, "max_depth": 1, "class_weight": None}),
]


def _run(*cmd: str) -> None:
    subprocess.run(list(cmd), cwd=ROOT, check=True)  # noqa: S603


def checkout_dataset(ref: str) -> None:
    """Switch ``data/raw/churn.csv`` to the dataset version recorded at git ref ``ref``."""
    _run("git", "checkout", ref, "--", POINTER)
    _run(sys.executable, "-m", "dvc", "checkout", POINTER)


def run_one(tag: str, experiment: str, model_type: str, overrides: dict[str, Any], work: Path) -> dict:
    params = load_params()
    hyper = {**params["model"].get(model_type, {}), **overrides}
    processed, models, reports = work / "processed", work / "models", work / "reports"

    if validate.main(["--data", str(DATA), "--report", str(reports / "validation.json")]) != 0:
        raise RuntimeError(f"dataset {tag} failed validation")
    preprocess.run(DATA, processed, params["split"]["test_size"], params["split"]["seed"])
    train.run(processed / "train.csv", models, model_type, hyper)

    os.environ["MLFLOW_EXPERIMENT"] = experiment
    os.environ["PIPELINE_RUN_ID"] = "seed_experiments"
    result = evaluate.run(processed / "test.csv", models, reports)
    passed, reasons = quality_gate.check(result["metrics"], params["quality_gate"])
    quality_gate.record_gate(result, passed, reasons)
    result.update(passed=passed, experiment=experiment, dataset=tag)
    print(f"  -> gate {'PASSED' if passed else 'FAILED'} {reasons or ''}")
    return result


def main() -> int:
    if not os.environ.get("MLFLOW_TRACKING_URI"):
        print("set MLFLOW_TRACKING_URI (e.g. http://localhost:5000) first")
        return 2
    original = subprocess.run(
        ["git", "rev-parse", "HEAD"],  # noqa: S607
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    results: list[dict] = []
    try:
        for tag in dict.fromkeys(t for t, *_ in PLAN):
            print(f"== dataset {tag}")
            checkout_dataset(tag)
            for t, experiment, model_type, overrides in PLAN:
                if t != tag:
                    continue
                print(f"-- {experiment}: {model_type} {overrides}")
                with tempfile.TemporaryDirectory() as tmp:
                    results.append(run_one(tag, experiment, model_type, overrides, Path(tmp)))
    finally:
        checkout_dataset(original)
        os.environ.pop("MLFLOW_EXPERIMENT", None)

    eligible = [r for r in results if r["passed"] and r["dataset"] == "data-v2.0"]
    best = max(eligible, key=lambda r: r["metrics"]["f1"])
    version = quality_gate.promote(best)
    print(
        f"\nseeded {len(results)} runs; champion = v{version} "
        f"(f1={best['metrics']['f1']}, run {best['run_id']})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
