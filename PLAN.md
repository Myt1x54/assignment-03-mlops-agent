# PLAN.md â€” Implementation Plan (S0 â€¦ S18)

Status values: `TODO` Â· `IN PROGRESS` Â· `AWAITING VERIFICATION` Â· `DONE (verified <date>)`.
Protocol: one step at a time; after each step Claude explains it and waits for the user to verify (see CLAUDE.md Â§1).
Decisions referenced as `D<n>` are in `DECISION.md`.

| Step | Title | Rubric area | Status |
|---|---|---|---|
| S0 | Planning documents | â€” | DONE (verified 2026-11-10) |
| S1 | Repository scaffold & reproducible environment | CI (5) | DONE (verified 2026-11-13) |
| S2 | Dataset generation & DVC versioning | DVC (15) | AWAITING VERIFICATION |
| S3 | Training code with MLflow logging & provenance | MLflow (15) | AWAITING VERIFICATION |
| S4 | Local infra: MLflow server (docker compose) + seeded experiments | MLflow (15) | AWAITING VERIFICATION |
| S5 | FastAPI inference service + Dockerfile | K8s (15) | AWAITING VERIFICATION |
| S6 | Kubernetes cluster (kind) + manifests + deployment | K8s (15) | AWAITING VERIFICATION |
| S7 | Airflow 3 + training DAG with quality gate | Airflow (15) | AWAITING VERIFICATION |
| S8 | Agent core: schemas, tool registry, logging, safety layer | Integration / Safety | AWAITING VERIFICATION |
| S9 | DVC tools | DVC (15) | TODO |
| S10 | MLflow tools | MLflow (15) | TODO |
| S11 | Airflow tools | Airflow (15) | TODO |
| S12 | Kubernetes tools (with confirmation + namespace guard) | K8s (15) | TODO |
| S13 | LLM orchestrator + CLI + mandatory scenario | Integration (15) | TODO |
| S14 | Test suite (â‰¥ 8, target ~25) | Testing (10) | TODO |
| S15 | GitHub repo + Actions CI | CI (5) | TODO |
| S16 | Failure injection & diagnosis (2+) | Testing / Demo | TODO |
| S17 | Demo scenarios & evidence capture | Demo (10) | TODO |
| S18 | Documentation, report, final review, submission | Submission | TODO |

---

## S0 â€” Planning documents
- **Goal:** Agree on scope, architecture, and workflow before writing code.
- **Deliverables:** `CLAUDE.md`, `AGENT.md`, `INSTRUCTION.md`, `PLAN.md`, `DECISION.md`.
- **Verify:** User reads the files and approves (or edits) the decisions marked *Proposed*, especially D2, D4, D11. â†’ D2 superseded by D25 (Gemini free tier); D4, D11 accepted.
- **Status:** DONE (verified 2026-11-10)

## S1 â€” Repository scaffold & reproducible environment
- **Goal:** Working Python project skeleton matching the required layout.
- **Tasks:**
  1. `git init`, `.gitignore` (venv, `.env`, `mlruns/`, data contents, kubeconfig, `__pycache__`).
  2. `uv init` â†’ `pyproject.toml` pinned to Python 3.12 (D1); dependency groups: `agent`, `train`, `api`, `dev`.
  3. Create folder tree (`agent/tools`, `api`, `dags`, `data`, `training`, `kubernetes`, `infra`, `tests`,
     `screenshots`, `docs/report`, `config`).
  4. `ruff` + `pytest` config in `pyproject.toml`; `.env.example` with placeholders only.
  5. `uv lock` + `uv sync`; a smoke test `tests/test_smoke.py`.
- **Acceptance:** `uv sync && uv run ruff check . && uv run pytest -q` passes on a clean clone.
- **Status:** DONE (verified 2026-11-13) â€” `uv sync` OK (Python 3.12.15), ruff clean, 5/5 smoke tests pass.

## S2 â€” Dataset generation & DVC versioning
- **Goal:** Real dataset versions with reproducible provenance (D4, D6).
- **Tasks:**
  1. `training/make_dataset.py` â€” deterministic synthetic churn dataset (seeded); `--version v1|v2|corrupt`.
  2. `dvc init`; local remote `../mlops-dvc-remote` (D6); track `data/raw/churn.csv`.
  3. Commit v1 â†’ git tag `data-v1.0`; regenerate v2 (more rows + drift) â†’ tag `data-v2.0`; `dvc push` both.
  4. `dvc.yaml` pipeline: `validate â†’ preprocess â†’ train â†’ evaluate` with `params.yaml`.
  5. Demonstrate reproducible checkout: `git checkout data-v1.0 -- data/raw/churn.csv.dvc && dvc checkout`.
- **Acceptance:** `dvc status` clean; `dvc diff data-v1.0 data-v2.0` shows modification; checking out v1 restores
  the identical md5.

## S3 â€” Training code with MLflow logging & provenance
- **Goal:** `training/` modules usable by DVC, Airflow, and manually (D5, D7).
- **Tasks:**
  1. `training/validate.py` (schema, nulls, ranges, row count), `preprocess.py`, `train.py`, `evaluate.py`.
  2. MLflow logging: params, metrics (accuracy, precision, recall, F1, ROC-AUC), model artifact, confusion matrix.
  3. Provenance tags on every run: `data.version`, `data.dvc_md5`, `data.path`, `git.commit`, `dvc.remote`,
     `pipeline.run_id` (when launched by Airflow).
  4. Register model `churn-classifier`; set alias `candidate`; quality gate sets `champion` (D10).
- **Acceptance:** A local run produces an MLflow run with all tags; values match `dvc`/`git` output.

## S4 â€” Local infra: MLflow server + seeded experiments
- **Goal:** Persistent MLflow tracking server and meaningful history for the agent to reason over (D8).
- **Tasks:**
  1. `infra/docker-compose.yml` service `mlflow` (sqlite backend, local artifact volume, port 5000).
  2. `scripts/seed_experiments.py`: â‰¥ 2 experiments, â‰¥ 8 runs across both dataset versions and 2â€“3 model types.
  3. Register 2+ model versions with aliases.
- **Acceptance:** MLflow UI shows experiments, runs, registry; screenshot saved to `screenshots/mlflow_*.png`.

## S5 â€” FastAPI inference service + Dockerfile
- **Goal:** Containerized prediction API (D24).
- **Tasks:**
  1. `api/main.py`: `/healthz` (liveness), `/readyz` (model loaded), `/predict`, `/version`; config from env.
  2. `scripts/export_model.py` pulls the `champion` model from MLflow into `model/` for the image build.
  3. `Dockerfile` (python:3.12-slim, non-root user, `APP_VERSION` build arg/label).
  4. Unit tests with FastAPI `TestClient`.
- **Acceptance:** `docker run -p 8000:8000 mlops-api:0.1.0` â†’ `/readyz` 200, `/predict` returns a prediction.

## S6 â€” Kubernetes cluster (kind) + manifests + deployment
- **Goal:** Inference service running on Kubernetes per spec (D11).
- **Tasks:**
  1. `kind create cluster --config infra/kind-config.yaml`; `kind load docker-image mlops-api:0.1.0 --name mlops`.
  2. Manifests: `namespace.yaml` (`mlops-demo`), `configmap.yaml`, `deployment.yaml` (2 replicas, probes,
     requests/limits, version labels), `service.yaml` (ClusterIP).
  3. Read-only ServiceAccount/RBAC for the agent + a separate role for allowed write ops (D13).
  4. `scripts/deploy.ps1` / `deploy.sh` helpers.
- **Acceptance:** `kubectl -n mlops-demo get deploy,pods,svc` shows 2/2 Ready; port-forward `/predict` works.

## S7 â€” Airflow 3 + training DAG with quality gate
- **Goal:** Real orchestrated training pipeline (D9, D10).
- **Tasks:**
  1. Custom Airflow image (`infra/airflow.Dockerfile`) with project deps; add Airflow services to compose
     (api-server, scheduler, dag-processor, postgres); mount `dags/`, `training/`, `data/`.
  2. `dags/training_pipeline.py` (`ml_training_pipeline`): `validate_data â†’ preprocess â†’ train â†’
     evaluate_and_log â†’ quality_gate â†’ promote_model`; `retries=2`, `retry_delay`, logging, `doc_md`.
  3. Quality gate raises `AirflowFailException` below threshold â†’ `promote_model` never runs.
  4. Validation failure raises `AirflowFailException` (no retries for data errors).
- **Acceptance:** Triggered run succeeds end-to-end and logs to MLflow; a run with corrupt data fails at
  `validate_data` and downstream tasks are `upstream_failed`. Screenshots saved.

## S8 â€” Agent core: schemas, registry, logging, safety
- **Goal:** Framework every tool plugs into (D3, D15, D17).
- **Tasks:**
  1. `agent/schemas.py`: `ToolResult`, `ToolStatus`, per-tool Pydantic input models, `AgentResponse`.
  2. `agent/tools/base.py`: `@tool` registration, JSON schema export for the LLM, timeout wrapper, error mapping.
  3. `agent/logging_utils.py`: JSON logs (`request_id`, `tool`, `status`, `duration_ms`), secret redaction.
  4. `agent/safety.py`: namespace allow-list, approved DAG list, confirmation-token workflow.
  5. `agent/config.py`: settings from env (pydantic-settings); `config/agent_policy.yaml`.
- **Acceptance:** Unit tests for timeout, error mapping, redaction, and permission checks pass.

## S9 â€” DVC tools
- **Goal:** â‰¥ 5 DVC tools per AGENT.md Â§3.1 (D14).
- **Acceptance:** Tools return correct data against the real repo; mocked-subprocess unit tests pass; a
  disallowed command is rejected.

## S10 â€” MLflow tools
- **Goal:** â‰¥ 5 MLflow tools per AGENT.md Â§3.2 using `MlflowClient`.
- **Acceptance:** Against the seeded server: best run by F1 is correct; compare returns a table; registry info
  shown; unreachable server â†’ `unavailable`.

## S11 â€” Airflow tools
- **Goal:** â‰¥ 5 Airflow tools per AGENT.md Â§3.3 using authenticated REST API v2.
- **Acceptance:** List/trigger/status/failed-task/log retrieval work against live Airflow; triggering a
  non-approved DAG is rejected; auth failure surfaces as `error` without leaking credentials.

## S12 â€” Kubernetes tools
- **Goal:** â‰¥ 7 K8s tools per AGENT.md Â§3.4 with confirmation + namespace guard (D12, D13).
- **Acceptance:** Live cluster inspection works; restart in `default` namespace â†’ `rejected`; restart without
  confirmation â†’ `confirmation_required`; with confirmation â†’ rollout restart observed.

## S13 â€” LLM orchestrator + CLI + mandatory scenario
- **Goal:** One conversational agent integrating all tools (D25, D16, D18, D19).
- **Tasks:**
  1. `agent/llm.py` (Gemini `google-genai` client wrapper with 429 backoff; swappable fake for tests) â€” **ask user for the Gemini API key here** (D25).
  2. `agent/orchestrator.py`: tool-use loop, parallel tool execution, confirmation pause, iteration/time budget,
     evidence IDs, deterministic evidence table.
  3. `agent/prompts.py`: system prompt from AGENT.md Â§2.
  4. `agent/main.py`: interactive CLI (rich), `--once "<question>"`, transcript export to `docs/report/transcripts/`.
- **Acceptance:** Mandatory integration scenario answered with Facts / Uncertainties / Recommendations; no DAG
  triggered; transcript saved.

## S14 â€” Test suite
- **Goal:** â‰¥ 8 required tests (target ~25) â€” all external services mocked.
- **Required tests (minimum):**
  1. Tool selection: fake LLM picks `mlflow_best_run` and result flows back.
  2. Multi-tool request executes MLflow + DVC + K8s + Airflow tools.
  3. Tool timeout â†’ `status=timeout`, agent reports it.
  4. Invalid tool input â†’ validation error returned to LLM, no crash.
  5. Service failure (MLflow down) â†’ `unavailable`.
  6. **No invention:** with K8s unavailable, final answer contains no pod/replica facts and evidence table
     marks K8s `unavailable`.
  7. **Unauthorized K8s modification rejected** (wrong namespace / no confirmation).
  8. Non-approved DAG trigger rejected.
  9. Structured log contains tool, status, duration, request_id and no secret values.
  10. Quality gate logic blocks promotion; data validation catches corrupt data.
  11. FastAPI `/readyz` and `/predict`.
- **Acceptance:** `uv run pytest -q` green; coverage report generated.

## S15 â€” GitHub repo + Actions CI
- **Goal:** Public repo `assignment-03-mlops-agent` with CI on PRs (D20, D22).
- **Tasks:** `.github/workflows/ci.yml` (uv setup, ruff lint + format check, pytest, optional manifest
  validation with `kubeconform`); push `main`; open a PR to prove CI runs on pull requests.
- **Acceptance:** Green check on a pull request.

## S16 â€” Failure injection & diagnosis
- **Goal:** â‰¥ 2 deliberate failures diagnosed by the agent (D21).
- **Failures:**
  - **F1 (Kubernetes):** broken ConfigMap `MODEL_PATH` â†’ readiness probe fails â†’ Pods not Ready â†’ service has no
    endpoints. Agent diagnoses via pods/events/logs.
  - **F2 (Airflow/Data):** corrupt dataset version (nulls + out-of-range values) â†’ `validate_data` fails,
    downstream `upstream_failed`, no promotion. Agent finds the failed task and log reason.
  - **F3 (optional):** MLflow server stopped â†’ agent reports unavailable without inventing.
- **Acceptance:** `docs/report/failures.md` with symptom, agent transcript, root cause, resolution, verification.

## S17 â€” Demo scenarios & evidence capture
- **Goal:** Five tested conversations with real tool evidence.
- **Scenarios:** dataset provenance Â· model selection Â· pipeline execution Â· deployment diagnosis Â·
  multi-tool reasoning (mandatory scenario).
- **Acceptance:** Transcripts in `docs/report/transcripts/`, screenshots of DVC/MLflow/Airflow/K8s in `screenshots/`.

## S18 â€” Documentation, report, final review, submission
- **Tasks:** `README.md` (install, env config, launch each service, tests, agent usage), `architecture.md`
  (diagram + data flow + tool permissions), `docs/report/REPORT.md` (all required sections), limitations &
  improvements, secret scan of git history, final rubric walk-through in `INSTRUCTION.md`.
- **Acceptance:** Fresh-clone dry run following README works; every rubric box ticked; user submits the repo URL.
