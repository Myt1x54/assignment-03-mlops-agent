"""Capture evidence screenshots of all four systems into ``screenshots/`` (PLAN S17).

Uses headless Chromium via Playwright without adding it to the project dependencies:

    uv run --with playwright playwright install chromium
    uv run --with playwright python scripts/capture_screenshots.py

MLflow and Airflow are captured from their web UIs. Kubernetes and DVC have no UI here, so the output of
read-only CLI commands is rendered as a terminal-style HTML page and captured too.
"""

from __future__ import annotations

import html
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values
from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "screenshots"
ENV = dotenv_values(ROOT / ".env")
MLFLOW = ENV.get("MLFLOW_TRACKING_URI") or "http://localhost:5000"
AIRFLOW = ENV.get("AIRFLOW_BASE_URL") or "http://localhost:8080"

TERMINAL_SETS = {
    "kubernetes_resources": [
        ["kubectl", "-n", "mlops-demo", "get", "deploy,rs,pods,svc,endpointslices,configmap", "-o", "wide"],
        ["kubectl", "-n", "mlops-demo", "describe", "deploy", "churn-api"],
    ],
    "kubernetes_rbac_guard": [
        [
            "kubectl",
            "auth",
            "can-i",
            "patch",
            "deployments",
            "-n",
            "mlops-demo",
            "--as=system:serviceaccount:mlops-demo:mlops-agent",
        ],
        [
            "kubectl",
            "auth",
            "can-i",
            "delete",
            "deployments",
            "-n",
            "mlops-demo",
            "--as=system:serviceaccount:mlops-demo:mlops-agent",
        ],
        [
            "kubectl",
            "auth",
            "can-i",
            "list",
            "pods",
            "-n",
            "kube-system",
            "--as=system:serviceaccount:mlops-demo:mlops-agent",
        ],
        [
            "kubectl",
            "auth",
            "can-i",
            "get",
            "secrets",
            "-n",
            "mlops-demo",
            "--as=system:serviceaccount:mlops-demo:mlops-agent",
        ],
    ],
    "dvc_versions": [
        ["git", "tag", "-n1", "--list", "data-*"],
        [sys.executable, "-m", "dvc", "diff", "data-v1.0", "data-v2.0"],
        [sys.executable, "-m", "dvc", "status"],
        ["git", "show", "data-v1.0:data/raw/churn.csv.dvc"],
        ["git", "show", "data-v2.0:data/raw/churn.csv.dvc"],
        [sys.executable, "-m", "dvc", "remote", "list"],
    ],
    "dvc_pipeline": [
        [sys.executable, "-m", "dvc", "dag"],
        [sys.executable, "-m", "dvc", "metrics", "show"],
        [sys.executable, "-m", "dvc", "params", "diff", "data-v1.0", "--all"],
    ],
}


def terminal_html(title: str, commands: list[list[str]]) -> str:
    blocks = []
    for cmd in commands:
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False)  # noqa: S603
        shown = " ".join("dvc" if c == sys.executable else c for c in cmd).replace("dvc -m dvc", "dvc")
        output = (proc.stdout + proc.stderr).strip() or "(no output)"
        blocks.append(f'<div class="cmd">$ {html.escape(shown)}</div><pre>{html.escape(output)}</pre>')
    return f"""<html><head><style>
      body {{ background:#0d1117; color:#c9d1d9; font:13px Consolas,monospace; margin:24px; width:1500px }}
      h1 {{ font:600 18px Segoe UI,sans-serif; color:#58a6ff }}
      .cmd {{ color:#7ee787; margin-top:16px; font-weight:bold }}
      pre {{ margin:6px 0 0; white-space:pre-wrap }}
    </style></head><body><h1>{html.escape(title)}</h1>{"".join(blocks)}</body></html>"""


def shot(
    page: Page, name: str, url: str | None = None, wait_text: str | None = None, delay: int = 2500
) -> None:
    if url:
        page.goto(url, wait_until="load")
    if wait_text:
        page.get_by_text(wait_text).first.wait_for(timeout=20_000)
    page.wait_for_timeout(delay)
    page.screenshot(path=str(OUT / f"{name}.png"), full_page=True)
    print(f"saved screenshots/{name}.png")


def close_mlflow_assistant(page: Page) -> None:
    """Hide the MLflow 3 'Assistant' side panel so the runs table gets the full width."""
    page.add_style_tag(
        content="[class*='assistant' i], [data-testid*='assistant' i] { display:none !important }"
    )


def training_runs(page: Page, name: str, exp_id: str) -> None:
    page.goto(f"{MLFLOW}/#/experiments/{exp_id}", wait_until="load")
    page.get_by_text("Training runs").first.click()
    page.wait_for_timeout(3000)
    close_mlflow_assistant(page)
    shot(page, name, delay=1500)


def mlflow(page: Page) -> None:
    training_runs(page, "mlflow_experiment_runs", "1")
    training_runs(page, "mlflow_hyperparam_search", "2")
    shot(page, "mlflow_model_registry", f"{MLFLOW}/#/models/churn-classifier", wait_text="churn-classifier")
    shot(page, "mlflow_champion_run", f"{MLFLOW}/#/experiments/1/runs/77b89e8dbb8846f9bd182e2a5c28ec62")
    shot(page, "mlflow_failed_gate_run", f"{MLFLOW}/#/experiments/2/runs/ab98797ff7dd4c218fffffa1eb3d06a9")


def airflow(page: Page) -> None:
    page.goto(f"{AIRFLOW}/auth/login/", wait_until="load")
    page.fill("input[name='username']", ENV["AIRFLOW_USERNAME"])
    page.fill("input[name='password']", ENV["AIRFLOW_PASSWORD"])
    page.keyboard.press("Enter")
    page.wait_for_timeout(4000)
    shot(page, "airflow_dags", f"{AIRFLOW}/dags", wait_text="ml_training_pipeline", delay=4000)
    shot(page, "airflow_dag_runs", f"{AIRFLOW}/dags/ml_training_pipeline/runs", delay=4000)
    shot(page, "airflow_dag_graph", f"{AIRFLOW}/dags/ml_training_pipeline?tab=graph", delay=5000)


def main() -> int:
    OUT.mkdir(exist_ok=True)
    only = set(sys.argv[1:])
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        if not only or "mlflow" in only:
            mlflow(page)
        if not only or "airflow" in only:
            airflow(page)
        if not only or "terminal" in only:
            for name, commands in TERMINAL_SETS.items():
                page.set_content(terminal_html(name.replace("_", " "), commands))
                shot(page, name, delay=300)
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
