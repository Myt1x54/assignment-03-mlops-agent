"""Deliberate failure injection for the demo / report (PLAN S16, DECISION D21).

    uv run python scripts/failures.py inject  f1   # K8s: ConfigMap MODEL_PATH -> missing file + restart
    uv run python scripts/failures.py restore f1
    uv run python scripts/failures.py inject  f2   # Data/Airflow: corrupt batch -> trigger DAG on it
    uv run python scripts/failures.py restore f2   # removes the corrupt batch file
    uv run python scripts/failures.py inject  f3   # MLflow server stopped (docker stop mlflow)
    uv run python scripts/failures.py restore f3
    uv run python scripts/failures.py status

This script is an *operator* tool used to create the failure; the agent only diagnoses (read-only tools).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import requests
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
NAMESPACE, DEPLOYMENT, CONFIGMAP = "mlops-demo", "churn-api", "churn-api-config"
GOOD_MODEL_PATH, BAD_MODEL_PATH = "/app/model/model.joblib", "/app/model/model-v2.joblib"
BATCH = ROOT / "data" / "incoming" / "churn_batch.csv"
ENV = dotenv_values(ROOT / ".env")


def _k8s():
    from kubernetes import client, config

    config.load_kube_config(context=ENV.get("K8S_CONTEXT") or "kind-mlops")
    return client.CoreV1Api(), client.AppsV1Api()


def _restart(apps) -> None:
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    body = {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": stamp}}}}}
    apps.patch_namespaced_deployment(DEPLOYMENT, NAMESPACE, body)


def f1(inject: bool) -> None:
    core, apps = _k8s()
    path = BAD_MODEL_PATH if inject else GOOD_MODEL_PATH
    core.patch_namespaced_config_map(CONFIGMAP, NAMESPACE, {"data": {"MODEL_PATH": path}})
    _restart(apps)
    print(f"F1 {'injected' if inject else 'restored'}: ConfigMap MODEL_PATH={path}; rollout restart issued")
    if not inject:
        subprocess.run(  # noqa: S603
            ["kubectl", "-n", NAMESPACE, "rollout", "status", f"deploy/{DEPLOYMENT}", "--timeout=180s"],  # noqa: S607
            check=False,
        )


def _airflow_token() -> tuple[str, str]:
    base = ENV.get("AIRFLOW_BASE_URL") or "http://localhost:8080"
    resp = requests.post(
        f"{base}/auth/token",
        json={"username": ENV["AIRFLOW_USERNAME"], "password": ENV["AIRFLOW_PASSWORD"]},
        timeout=10,
    )
    resp.raise_for_status()
    return base, resp.json()["access_token"]


def f2(inject: bool) -> None:
    if not inject:
        BATCH.unlink(missing_ok=True)
        print(f"F2 restored: removed {BATCH.relative_to(ROOT)}")
        return
    from training.make_dataset import generate, write_csv

    write_csv(generate("corrupt"), BATCH)
    base, token = _airflow_token()
    headers = {"Authorization": f"Bearer {token}"}
    conf = {"data_path": BATCH.relative_to(ROOT).as_posix()}
    run = requests.post(
        f"{base}/api/v2/dags/ml_training_pipeline/dagRuns",
        headers=headers,
        json={"logical_date": None, "conf": conf, "note": "F2 failure injection: corrupt incoming batch"},
        timeout=10,
    )
    run.raise_for_status()
    run_id = run.json()["dag_run_id"]
    print(f"F2 injected: wrote corrupt batch, triggered {run_id} with conf {conf}; waiting...")
    for _ in range(60):
        time.sleep(5)
        state = requests.get(
            f"{base}/api/v2/dags/ml_training_pipeline/dagRuns/{run_id}", headers=headers, timeout=10
        ).json()["state"]
        if state in ("success", "failed"):
            print(f"F2 run finished: state={state}")
            return
    print("F2 run still running; check Airflow UI")


def f3(inject: bool) -> None:
    action = "stop" if inject else "start"
    subprocess.run(["docker", action, "mlflow"], check=True)  # noqa: S603, S607
    print(f"F3 {'injected' if inject else 'restored'}: docker {action} mlflow")


def status() -> None:
    core, apps = _k8s()
    cm = core.read_namespaced_config_map(CONFIGMAP, NAMESPACE).data
    d = apps.read_namespaced_deployment(DEPLOYMENT, NAMESPACE)
    print(
        json.dumps(
            {
                "configmap_MODEL_PATH": cm.get("MODEL_PATH"),
                "replicas": {"desired": d.spec.replicas, "ready": d.status.ready_replicas},
                "corrupt_batch_present": BATCH.exists(),
            },
            indent=2,
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("action", choices=["inject", "restore", "status"])
    parser.add_argument("failure", nargs="?", choices=["f1", "f2", "f3"])
    args = parser.parse_args()
    if args.action == "status":
        status()
        return 0
    if not args.failure:
        parser.error("failure id required")
    {"f1": f1, "f2": f2, "f3": f3}[args.failure](args.action == "inject")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    sys.exit(main())
