# Assignment 03: Production-Ready MLOps AI Agent

A conversational agent that inspects and operates a complete ML system built on **DVC, MLflow, Apache Airflow 3
and Kubernetes**, and answers **only from real tool evidence**.

You ask in plain English, for example *"Find the best recent model, identify the dataset version used to train
it, check the deployment status, and explain whether retraining is necessary."* The agent (Gemini via function
calling) then:
1. picks from **27 typed tools**;
2. runs independent calls in parallel;
3. correlates the results;
4. answers in three sections: **Facts (with evidence) / Uncertainties / Recommended actions**;
5. ends with an evidence table generated from the actual tool results.

State-changing actions (restart, scale, trigger training) need **your** typed `yes`. The LLM can never approve
them itself.

| Document | Content |
|---|---|
| [`architecture.md`](architecture.md) | Diagrams, request flow, all 27 tools with permissions, failure contract |
| [`docs/report/REPORT.md`](docs/report/REPORT.md) | Technical report: evidence, five demo conversations, failure investigations, limitations |
| [`AGENT.md`](AGENT.md) | Runtime agent specification (reasoning rules, tool catalogue, retraining policy) |
| [`DECISION.md`](DECISION.md) | Architecture decision log D0-D38 |
| [`PLAN.md`](PLAN.md) | Implementation steps S0-S18 |

---

## 1. Repository layout

```
agent/            conversational agent: orchestrator, Gemini client, prompts, safety, logging, CLI
  tools/          dvc_tools, mlflow_tools, airflow_tools, kubernetes_tools (+ base registry)
api/              FastAPI inference service (churn prediction)
dags/             Airflow 3 DAG: ml_training_pipeline
training/         make_dataset, validate, preprocess, train, evaluate (MLflow), quality_gate, provenance
kubernetes/       namespace, ConfigMap, Deployment, Service, RBAC
infra/            docker-compose (MLflow + Airflow), Airflow image, kind cluster config
scripts/          seed_experiments, export_model, bootstrap_data, failures (injection)
config/           agent_policy.yaml (retraining thresholds, safety limits)
tests/            154 pytest tests (144 unit, fully mocked + 10 live integration, opt-in)
docs/report/      technical report, failure investigations, conversation transcripts
screenshots/      evidence from MLflow, Airflow, Kubernetes, DVC
```

## 2. Prerequisites

| Tool | Version used | Install (Windows) |
|---|---|---|
| Docker Desktop | 29.x | https://www.docker.com/products/docker-desktop |
| uv | 0.13 | `winget install astral-sh.uv` |
| kubectl | 1.33+ | `winget install Kubernetes.kubectl` |
| kind | 0.30+ | `winget install Kubernetes.kind` |
| git | 2.4x | `winget install Git.Git` |
| Gemini API key (free) | - | https://aistudio.google.com/apikey |

Python **3.12** is installed automatically by `uv`; no system Python is needed. Linux and macOS work the same
way; only the install commands differ.

## 3. Setup

```bash
git clone https://github.com/Myt1x54/assignment-03-mlops-agent.git
cd assignment-03-mlops-agent
uv sync                                  # Python 3.12 + locked dependencies (uv.lock)
cp .env.example .env                     # then edit .env (see below)
```

`.env` is git-ignored. Values to set:

| Variable | Meaning |
|---|---|
| `GEMINI_API_KEY` | Google AI Studio key (free tier). Only needed to *talk* to the agent, not for tests |
| `AGENT_MODEL` | Gemini model id, default `gemini-flash-latest` |
| `AIRFLOW_USERNAME` / `AIRFLOW_PASSWORD` | Admin user created by `airflow-init`; the agent logs in with it |
| `AIRFLOW_JWT_SECRET` | Any long random string shared by the Airflow components |
| `MLFLOW_TRACKING_URI`, `AIRFLOW_BASE_URL` | Defaults `http://localhost:5000`, `http://localhost:8080` |
| `K8S_CONTEXT`, `K8S_NAMESPACE`, `K8S_ALLOWED_NAMESPACES` | Defaults `kind-mlops`, `mlops-demo`, `mlops-demo` |
| `AIRFLOW_APPROVED_DAGS` | DAGs the agent may trigger (with confirmation), default `ml_training_pipeline` |

## 4. Data (DVC)

Two dataset versions are tracked with DVC and tagged in git: `data-v1.0` (5,000 rows) and `data-v2.0`
(8,000 rows, drifted). The DVC remote is a local folder next to the repo (`../mlops-dvc-remote`), so on a fresh
clone you rebuild it. The generator is seeded, so the script regenerates both versions **byte-for-byte** and
verifies each md5 against the pointer stored at its git tag:

```bash
uv run python scripts/bootstrap_data.py   # regenerate + verify md5 + fill DVC cache + dvc push
uv run dvc diff data-v1.0 data-v2.0       # -> modified: data/raw/churn.csv
uv run dvc repro                          # rebuild pipeline outputs (processed data, model, reports)
uv run dvc status                         # -> "Data and pipelines are up to date."
```

Notes from a fresh-clone dry run:
- On **Windows**, clone into a short path (e.g. `C:\src\assignment-03-mlops-agent`) or enable long paths. DVC's run
  cache uses deep paths, and a very deep clone location exceeds the 260-character `MAX_PATH` limit.
- After `dvc repro`, `dvc.lock` differs only in the hash of `models/train_info.json`, which records wall-clock
  training time. The trained model (`models/model.joblib`) is byte-identical to the committed one.

To reproduce an older version:

```bash
git checkout data-v1.0 -- data/raw/churn.csv.dvc && uv run dvc checkout data/raw/churn.csv.dvc
git checkout HEAD -- data/raw/churn.csv.dvc && uv run dvc checkout data/raw/churn.csv.dvc
```

Run the training pipeline locally without Airflow, logging to `./mlflow.db`:

```bash
uv run dvc repro
```

## 5. Launch the platform

### 5.1 MLflow and Airflow (docker compose)

```bash
docker compose -f infra/docker-compose.yml --env-file .env up -d --build
```

| UI | URL | Login |
|---|---|---|
| MLflow | http://localhost:5000 | - |
| Airflow | http://localhost:8080 | `AIRFLOW_USERNAME` / `AIRFLOW_PASSWORD` from `.env` |

Seed MLflow with a meaningful history: 10 runs, 2 experiments, both dataset versions, 3 model types, one run that
fails the quality gate, and the best gate-passing v2 model promoted to `champion`:

```bash
# PowerShell: $env:MLFLOW_TRACKING_URI="http://localhost:5000"   bash: export MLFLOW_TRACKING_URI=http://localhost:5000
uv run python scripts/seed_experiments.py
```

### 5.2 Inference service on Kubernetes (kind)

```bash
kind create cluster --config infra/kind-config.yaml                 # cluster "mlops", context kind-mlops
uv run python scripts/export_model.py --alias champion              # MLflow champion -> model/
docker build -t mlops-api:0.1.0 .
kind load docker-image mlops-api:0.1.0 --name mlops
kubectl apply -f kubernetes/namespace.yaml && kubectl apply -f kubernetes/
kubectl -n mlops-demo rollout status deploy/churn-api
kubectl -n mlops-demo port-forward svc/churn-api 8000:80            # optional: http://localhost:8000/docs
```

The Deployment runs 2 replicas behind a ClusterIP Service. It has readiness (`/readyz`) and liveness
(`/healthz`) probes, CPU and memory requests and limits, config from a ConfigMap, a non-root user, a read-only
root filesystem, and a RollingUpdate with `maxUnavailable: 0`.

### 5.3 Run the training DAG

Trigger it from the Airflow UI, or ask the agent: *"run the training pipeline"*. The agent asks for your
confirmation first.

```
validate_data -> preprocess -> train -> evaluate_and_log -> quality_gate -> promote_model
```

- Transient steps retry twice with exponential backoff. `validate_data` and `quality_gate` fail fast.
- A failed check leaves everything downstream `upstream_failed`, so a bad model can never be promoted.
- `promote_model` moves `champion` only if the candidate beats the current champion's F1.

## 6. Talk to the agent

```bash
uv run python -m agent.main                                   # interactive
uv run python -m agent.main --once "Which model is deployed and which data trained it?"
```

Each session is saved as a markdown transcript in `docs/report/transcripts/`, with every tool call, its status
and the evidence table. JSON logs go to stderr: one line per tool call with `request_id`, `tool`, `status`,
`duration_ms`, and secrets redacted.

Example questions:

| Scenario | Ask |
|---|---|
| Dataset provenance | "Which dataset version trained the production model? Can you prove it and show how to reproduce it?" |
| Model selection | "Compare the runs and tell me which model should be in production and why." |
| Pipeline execution | "Run the training pipeline and report the status of each task." |
| Deployment diagnosis | "Is the churn-api deployment healthy? If not, find the root cause." |
| Multi-tool | "Find the best recent model, identify the dataset version used to train it, check the deployment status, and explain whether retraining is necessary." |

## 7. Tests and CI

```bash
uv run ruff check . && uv run ruff format --check .
uv run pytest -q                                    # 144 unit tests, all services mocked, ~10 s
uv run pytest -q --cov=agent --cov=training --cov=api
uv run pytest -q -m integration                     # 10 live checks against the running platform
```

GitHub Actions (`.github/workflows/ci.yml`) runs on every pull request and push to `main`:
- ruff lint and format check;
- the unit tests with coverage;
- `kubeconform` strict validation of the Kubernetes manifests.

No secrets are needed in CI.

## 8. Failure injection (for the demo)

```bash
uv run python scripts/failures.py inject f1    # ConfigMap MODEL_PATH -> missing file; new pod never Ready
uv run python scripts/failures.py inject f2    # corrupt incoming batch -> validate_data fails, nothing promoted
uv run python scripts/failures.py inject f3    # MLflow server stopped -> tools report "unavailable"
uv run python scripts/failures.py restore f1|f2|f3
```

The investigations are written up in [`docs/report/failures.md`](docs/report/failures.md).

## 9. Safety model (summary)

- Tools are typed: every input is a Pydantic model with `extra="forbid"`, so the LLM cannot pass fields such as
  `confirmed=true`.
- Allow-lists are enforced in code:
  - Kubernetes namespace `mlops-demo` only, for reads and writes;
  - Airflow DAG `ml_training_pipeline` only;
  - read-only `git`/`dvc` subcommands, run without a shell;
  - replica range 1-5; log tails capped.
- Human confirmation: write tools return `confirmation_required`. The orchestrator shows the exact action, and
  only on `yes` re-executes it with an HMAC token bound to that tool and those arguments. The token expires
  after 5 minutes and is never shown to the LLM.
- Intent guard: if your message names a namespace, deployment, DAG or replica count, a write call that
  targets anything else is rejected (`IntentMismatch`) before you are even asked.
- No invention:
  - failed tools are marked "Not verified" in the answer;
  - a retraining verdict without evidence for every policy criterion is marked *cannot be fully
    determined*;
  - a grounding check flags figures that appear in no tool result;
  - the evidence table is built from the actual tool results.
- Cluster RBAC (`kubernetes/rbac.yaml`) mirrors these rules: the agent's account has read access plus `patch` on
  deployments in one namespace, and no access to Secrets.
- Secrets live only in `.env`, are typed as `SecretStr`, and are redacted from logs, tool output and transcripts.
