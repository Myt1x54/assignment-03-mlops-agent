# INSTRUCTION.md — Assignment Requirements, Rubric Checklist, Prerequisites

Condensed from the official brief (Assignment No. 3, 100 marks). Deliverable: **one GitHub repository URL**.

---

## 1. What must be built

An AI agent that understands natural-language requests, chooses structured tools, executes authorized
operations against **DVC, MLflow, Airflow 3, and Kubernetes**, and explains results with evidence.

## 2. Rubric checklist

Tick each box only when it is implemented **and** evidenced (test, screenshot, or transcript).

### Task 1 — DVC data management (15)
- [x] ≥ 3 callable tools
- [x] Identify DVC-tracked datasets
- [x] Current dataset version + Git commit
- [x] Compare dataset versions
- [x] Detect whether tracked data changed
- [x] Explain how to reproduce a previous version; **demonstrate reproducible checkout**
- [x] Provenance read from recorded data (MLflow tags), not invented
- [x] Restricted to approved commands (subprocess allow-list)

### Task 2 — MLflow experiment intelligence (15)
- [x] ≥ 3 tools, using `MlflowClient` / MLflow Python API (no UI scraping)
- [x] List experiments and runs
- [x] Metrics, params, artifacts of a run
- [x] Best run by a specified metric
- [x] Compare two or more runs/experiments
- [x] Model registry versions/aliases
- [x] Explains selection; does not equate highest F1 with production-ready

### Task 3 — Airflow workflow (15)
- [x] ≥ 3 tools using Airflow 3 REST API with authenticated requests
- [x] List DAGs, DAG run status, trigger approved DAG, failed tasks, task logs/diagnostics
- [x] Training DAG: validation → preprocessing → training → evaluation/MLflow logging → quality gate
- [x] Retries, task dependencies, logging
- [x] Failed validation / quality gate blocks downstream promotion

### Task 4 — Kubernetes operations (15)
- [x] ≥ 3 tools: Pods/Deployments, Pod status + logs, Services, unhealthy Pods/restarts, deployed version
- [x] FastAPI inference service, containerized
- [x] Deployment with ≥ 2 replicas
- [x] ClusterIP Service
- [x] Readiness + liveness probes
- [x] CPU/memory requests and limits
- [x] ConfigMap for non-sensitive config
- [x] State-changing ops need explicit confirmation and are limited to the approved namespace

### Integration — multi-tool agent (15)
- [x] Single agent, structured tool inputs/outputs, LLM tool calling
- [x] Handles requests needing multiple tools
- [x] Mandatory scenario works: MLflow → DVC → K8s → Airflow → correlate → facts / uncertainties / actions
- [x] Retraining never auto-started; recommended only with evidence

### Testing, reliability, safety (10)
- [x] ≥ 8 meaningful pytest tests
- [x] Mocked responses for unavailable services
- [x] Tool selection + execution success tests
- [x] Timeout, invalid request, service-failure tests
- [x] Test: agent does not invent results when a tool is unavailable
- [x] Test: unauthorized Kubernetes modification rejected
- [x] Structured logs: tool name, status, duration, request id, no secrets
- [x] Two deliberately injected failures, diagnosed by the agent, documented

### GitHub + CI (5)
- [x] Reproducible Python env (`pyproject.toml` + lock file)
- [x] GitHub Actions: lint + tests on pull requests; external calls mocked

### Demonstration + defense (10)
- [x] Dataset provenance scenario
- [x] Model selection scenario
- [x] Pipeline execution scenario (real DAG run + task statuses)
- [x] Deployment diagnosis scenario (health + logs)
- [x] Multi-tool reasoning scenario
- [x] Architecture explanation, tool-choice justification, ≥ 1 unexpected failure diagnosed

### Submission documents
- [x] README: installation, env config, launching each service, running tests, talking to the agent
- [x] Technical report: architecture, tool definitions + permissions, evidence screenshots/exports from all four
      systems, five tested conversations with tool evidence, two failure investigations (root cause +
      resolution), limitations + improvements
- [x] No secrets committed anywhere (history included)

## 3. Required repository structure (from the brief, extended)

```
assignment-03-mlops-agent/
├── agent/ (main.py, orchestrator.py, schemas.py, tools/{dvc,mlflow,airflow,kubernetes}_tools.py)
├── api/main.py                     # FastAPI inference service
├── dags/training_pipeline.py       # Airflow 3 DAG
├── data/                           # DVC-tracked data (.dvc files in git, content in DVC remote)
├── training/train.py
├── kubernetes/ (deployment.yaml, service.yaml, configmap.yaml, namespace.yaml)
├── infra/docker-compose.yml        # MLflow + Airflow (added by us)
├── tests/
├── screenshots/
├── docs/report/                    # technical report, transcripts, failure investigations (added by us)
├── .github/workflows/ci.yml
├── Dockerfile, dvc.yaml, pyproject.toml, README.md, architecture.md
```

## 4. Local prerequisites (Windows 11)

| Tool | Status on this machine | Action |
|---|---|---|
| Python 3.12 (via uv) | present (uv-managed) | none |
| uv 0.13 | present | none |
| Git 2.51 | present | none |
| Docker Desktop 29.x | installed, **daemon not running** | start Docker Desktop |
| kubectl 1.36 | present, **no context** | created by kind in S6 |
| kind | **missing** | install (`winget install Kubernetes.kind`) — see D11 |
| GitHub account + empty repo | ? | user creates `assignment-03-mlops-agent` |
| Google AI Studio (Gemini) API key | user will provide at S13 | set `GEMINI_API_KEY` in `.env` — see D25 |

## 5. Hard rules from the brief

- Operational claims must be supported by actual tool responses.
- Cluster-changing operations: explicit confirmation + approved namespace only.
- Never submit API keys, kubeconfigs, service-account tokens or other secrets.
- CI must mock Kubernetes and other external calls.

## 3. Evidence map (final review, 2026-11-14)

| Rubric area | Where it is evidenced |
|---|---|
| DVC (15) | `agent/tools/dvc_tools.py` (7 tools), `tests/test_dvc_tools.py`, `screenshots/dvc_*.png`, transcript S1, `scripts/bootstrap_data.py` |
| MLflow (15) | `agent/tools/mlflow_tools.py` (6 tools), `tests/test_mlflow_tools.py`, `screenshots/mlflow_*.png`, transcript S2 |
| Airflow (15) | `dags/training_pipeline.py`, `agent/tools/airflow_tools.py` (5 tools), `tests/test_airflow_tools.py`, `tests/test_dag_structure.py`, `screenshots/airflow_*.png`, transcripts S3, F2 |
| Kubernetes (15) | `kubernetes/*.yaml`, `api/main.py`, `Dockerfile`, `agent/tools/kubernetes_tools.py` (9 tools), `tests/test_kubernetes_tools.py`, `screenshots/kubernetes_*.png`, transcripts S4, F1 |
| Integration (15) | `agent/orchestrator.py`, `agent/llm.py`, `agent/prompts.py`, transcript S5 (mandatory scenario), `tests/test_orchestrator.py` |
| Testing & safety (10) | 144 unit + 10 integration tests; `agent/safety.py`, `agent/logging_utils.py`; `docs/report/failures.md` (F1, F2, F3, U1) |
| GitHub + CI (5) | `pyproject.toml` + `uv.lock`, `.github/workflows/ci.yml`, CI run on the `submission-review` pull request |
| Demo + defense (10) | `docs/report/transcripts/` (S1-S5, F1-F3, U1), `docs/report/REPORT.md` §9, `architecture.md` |
| Documents | `README.md`, `docs/report/REPORT.md`, `architecture.md` |

