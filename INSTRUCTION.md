# INSTRUCTION.md — Assignment Requirements, Rubric Checklist, Prerequisites

Condensed from the official brief (Assignment No. 3, 100 marks). Deliverable: **one GitHub repository URL**.

---

## 1. What must be built

An AI agent that understands natural-language requests, chooses structured tools, executes authorized
operations against **DVC, MLflow, Airflow 3, and Kubernetes**, and explains results with evidence.

## 2. Rubric checklist

Tick each box only when it is implemented **and** evidenced (test, screenshot, or transcript).

### Task 1 — DVC data management (15)
- [ ] ≥ 3 callable tools
- [ ] Identify DVC-tracked datasets
- [ ] Current dataset version + Git commit
- [ ] Compare dataset versions
- [ ] Detect whether tracked data changed
- [ ] Explain how to reproduce a previous version; **demonstrate reproducible checkout**
- [ ] Provenance read from recorded data (MLflow tags), not invented
- [ ] Restricted to approved commands (subprocess allow-list)

### Task 2 — MLflow experiment intelligence (15)
- [ ] ≥ 3 tools, using `MlflowClient` / MLflow Python API (no UI scraping)
- [ ] List experiments and runs
- [ ] Metrics, params, artifacts of a run
- [ ] Best run by a specified metric
- [ ] Compare two or more runs/experiments
- [ ] Model registry versions/aliases
- [ ] Explains selection; does not equate highest F1 with production-ready

### Task 3 — Airflow workflow (15)
- [ ] ≥ 3 tools using Airflow 3 REST API with authenticated requests
- [ ] List DAGs, DAG run status, trigger approved DAG, failed tasks, task logs/diagnostics
- [ ] Training DAG: validation → preprocessing → training → evaluation/MLflow logging → quality gate
- [ ] Retries, task dependencies, logging
- [ ] Failed validation / quality gate blocks downstream promotion

### Task 4 — Kubernetes operations (15)
- [ ] ≥ 3 tools: Pods/Deployments, Pod status + logs, Services, unhealthy Pods/restarts, deployed version
- [ ] FastAPI inference service, containerized
- [ ] Deployment with ≥ 2 replicas
- [ ] ClusterIP Service
- [ ] Readiness + liveness probes
- [ ] CPU/memory requests and limits
- [ ] ConfigMap for non-sensitive config
- [ ] State-changing ops need explicit confirmation and are limited to the approved namespace

### Integration — multi-tool agent (15)
- [ ] Single agent, structured tool inputs/outputs, LLM tool calling
- [ ] Handles requests needing multiple tools
- [ ] Mandatory scenario works: MLflow → DVC → K8s → Airflow → correlate → facts / uncertainties / actions
- [ ] Retraining never auto-started; recommended only with evidence

### Testing, reliability, safety (10)
- [ ] ≥ 8 meaningful pytest tests
- [ ] Mocked responses for unavailable services
- [ ] Tool selection + execution success tests
- [ ] Timeout, invalid request, service-failure tests
- [ ] Test: agent does not invent results when a tool is unavailable
- [ ] Test: unauthorized Kubernetes modification rejected
- [ ] Structured logs: tool name, status, duration, request id, no secrets
- [ ] Two deliberately injected failures, diagnosed by the agent, documented

### GitHub + CI (5)
- [ ] Reproducible Python env (`pyproject.toml` + lock file)
- [ ] GitHub Actions: lint + tests on pull requests; external calls mocked

### Demonstration + defense (10)
- [ ] Dataset provenance scenario
- [ ] Model selection scenario
- [ ] Pipeline execution scenario (real DAG run + task statuses)
- [ ] Deployment diagnosis scenario (health + logs)
- [ ] Multi-tool reasoning scenario
- [ ] Architecture explanation, tool-choice justification, ≥ 1 unexpected failure diagnosed

### Submission documents
- [ ] README: installation, env config, launching each service, running tests, talking to the agent
- [ ] Technical report: architecture, tool definitions + permissions, evidence screenshots/exports from all four
      systems, five tested conversations with tool evidence, two failure investigations (root cause +
      resolution), limitations + improvements
- [ ] No secrets committed anywhere (history included)

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
