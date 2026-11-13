# DECISION.md — Decision Log (D0 … Dn)

Format: each decision has **Context → Decision → Alternatives → Rationale → Status**.
Status values: `Proposed` (awaiting user approval) · `Accepted` · `Superseded by D<n>`.
Decisions are never renumbered or deleted.

---

## D0 — Step-gated workflow with user verification
- **Context:** The user wants to understand and approve each piece of work.
- **Decision:** Implement PLAN steps S0..S18 one at a time; after each step explain it and wait for verification.
  Every technical choice is logged here.
- **Status:** Accepted (requested by user).

## D1 — Python 3.12 managed by `uv`
- **Context:** Machine has Python 3.14 as default; Airflow 3, MLflow, and some scientific wheels lag on 3.14.
- **Decision:** Pin `requires-python = ">=3.12,<3.13"`; use `uv` for env + lockfile (`uv.lock`).
- **Alternatives:** pip + requirements.txt; Poetry; conda.
- **Rationale:** `uv` is already installed, fast, produces a lockfile → reproducible env (rubric) and simple CI.
- **Status:** Accepted (2026-11-10)

## D2 — LLM: Anthropic Claude (`claude-opus-5-5`) via the official `anthropic` Python SDK
- **Context:** Agent needs native structured tool calling.
- **Decision:** Use the Messages API with client-defined tools. Model configurable via `AGENT_MODEL` env var,
  default `claude-opus-5-5`; key from `ANTHROPIC_API_KEY` (never committed). Handle `refusal` stop reason and
  enable server-side fallbacks.
- **Alternatives:** OpenAI, local Ollama model (free but weak tool use), LangChain abstraction.
- **Rationale:** Strong multi-step tool use, parallel tool calls, strict JSON-schema tool inputs.
  The LLM client sits behind a small interface so tests use a scripted fake LLM (no API cost in CI).
- **Status:** Superseded by D25 (user requested a free model, 2026-11-10)

## D3 — Hand-written orchestrator instead of an agent framework
- **Context:** Brief allows a framework or direct tool calling.
- **Decision:** Implement the tool-use loop manually in `agent/orchestrator.py`.
- **Alternatives:** LangChain/LangGraph, SDK Tool Runner.
- **Rationale:** We need hard control points the grader can see: Pydantic validation, permission checks,
  human-confirmation pause for K8s writes, per-tool timeouts, evidence IDs, structured logs. A manual loop
  makes these explicit and trivially testable with a fake LLM.
- **Status:** Accepted (2026-11-10)

## D4 — Dataset: deterministic synthetic customer-churn data (binary classification)
- **Context:** Need multiple dataset versions, a drift story, and a corrupt version for failure injection.
- **Decision:** `training/make_dataset.py` generates a seeded tabular churn dataset: `v1` (5k rows),
  `v2` (8k rows, shifted feature distribution), `corrupt` (nulls/out-of-range values, never tagged as release).
- **Alternatives:** UCI/Kaggle datasets (download + licensing, less control over drift).
- **Rationale:** Fully reproducible offline, no external download, and lets us engineer realistic scenarios
  (newer data version → retraining evidence; corrupt data → validation failure). F1 is meaningful for binary classes.
  Features mirror a realistic telecom-churn schema (tenure, monthly charges, contract type, support calls, …) and
  the class balance is imbalanced (~25 % churn) so F1 is a meaningful selection metric. The generator and its seed
  are committed, so any grader can regenerate byte-identical data and confirm the DVC md5.
- **Status:** Accepted (2026-11-10) — user delegated the choice; chosen as the option that best covers the rubric.

## D5 — Model: scikit-learn (LogisticRegression, RandomForest, GradientBoosting)
- **Rationale:** CPU-fast, small artifacts, easy to serve; several model types make run comparison meaningful.
- **Status:** Accepted (2026-11-10)

## D6 — DVC remote: local directory outside the repo
- **Decision:** Remote `localremote` at `../mlops-dvc-remote`; dataset releases marked by git tags
  `data-v1.0`, `data-v2.0`.
- **Alternatives:** S3/GDrive/Azure remote (requires credentials → secret-handling risk).
- **Rationale:** No secrets, works offline, still demonstrates push/pull/checkout. README explains how to switch remotes.
- **Status:** Accepted (2026-11-10)

## D7 — Provenance link between MLflow runs and DVC versions = MLflow run tags
- **Decision:** Every training run records `data.version`, `data.dvc_md5`, `data.path`, `git.commit`,
  `dvc.remote`, and (if applicable) `pipeline.dag_run_id`.
- **Rationale:** The brief requires the agent to read *recorded* provenance. Tags make
  “which dataset trained run X” a lookup, and the DVC tool can verify the md5 against the Git revision.
- **Status:** Accepted (2026-11-10)

## D8 — MLflow tracking server in Docker Compose (SQLite backend, local artifacts)
- **Decision:** `mlflow server` container on `localhost:5000`, backend `sqlite:///mlflow.db`, artifacts in a volume.
  Agent uses `MlflowClient` only. Model registry `churn-classifier` with aliases `candidate` / `champion`.
- **Alternatives:** Postgres backend (heavier), file store (no registry support).
- **Status:** Accepted (2026-11-10)

## D9 — Airflow 3 via Docker Compose with a custom image; REST API v2 + JWT auth
- **Decision:** Extend `apache/airflow:3.x` with project dependencies; services: postgres, api-server, scheduler,
  dag-processor (LocalExecutor). Agent authenticates by obtaining a JWT from `/auth/token` using credentials from
  env vars, then calls `/api/v2/...` with `requests`.
- **Alternatives:** Native Airflow install (unsupported on Windows), Astro CLI.
- **Rationale:** Only practical way to run Airflow 3 on Windows; matches “authenticated requests” requirement.
- **Status:** Accepted (2026-11-10)

## D10 — Training DAG semantics
- **Decision:** `validate_data → preprocess → train → evaluate_and_log → quality_gate → promote_model`.
  `retries=2`, `retry_delay=30s` for transient errors; data-validation and quality-gate failures raise
  `AirflowFailException` (fail immediately, no retries). Gate threshold `min_f1` in `params.yaml` (default 0.80).
  Only `promote_model` sets the `champion` alias.
- **Rationale:** Guarantees a failed validation/gate can never promote a model, which the brief explicitly requires.
- **Status:** Accepted (2026-11-10)

## D11 — Local Kubernetes: kind (Kubernetes-in-Docker)
- **Decision:** Create cluster `mlops` with kind; load the locally built image with `kind load docker-image`.
- **Alternatives:** Docker Desktop built-in Kubernetes (simpler toggle, but less reproducible), minikube.
- **Rationale:** Reproducible from a script, no image registry needed, works in CI if ever required.
- **Requires:** installing kind (`winget install Kubernetes.kind`).
- **Status:** Accepted (2026-11-10)

## D12 — Kubernetes access through the official `kubernetes` Python client
- **Alternatives:** shelling out to `kubectl`.
- **Rationale:** Structured objects (no text parsing), easy to mock in tests, proper timeouts.
- **Status:** Accepted (2026-11-10)

## D13 — Kubernetes write-safety model
- **Decision:** Only two write tools exist (`k8s_restart_deployment`, `k8s_scale_deployment`, replicas 1..5).
  Guards, enforced in code: (1) namespace must be `mlops-demo`; (2) first call returns
  `confirmation_required` with a one-time action id; (3) the orchestrator asks the **human** in the CLI and only
  then re-executes with the token. The LLM cannot produce a valid token. RBAC on the cluster mirrors this.
- **Status:** Accepted (2026-11-10)

## D14 — DVC interface via allow-listed subprocess calls
- **Decision:** `subprocess.run([...], shell=False, timeout=...)` restricted to an allow-list of read-only
  `dvc`/`git` subcommands. Reproduction demo checks out a version into an isolated scratch `git worktree`
  so the main workspace is never modified by the agent.
- **Status:** Accepted (2026-11-10)

## D15 — Uniform `ToolResult` envelope with evidence IDs
- **Decision:** All tools return `{tool, request_id, status, data, error, duration_ms, evidence_id}`
  (see AGENT.md §4). Status ∈ `ok | error | timeout | unavailable | rejected | confirmation_required`.
- **Status:** Accepted (2026-11-10)

## D16 — Anti-hallucination controls
- **Decision:** (1) System-prompt rules; (2) failed tools returned to the LLM with `is_error=true` and explicit
  “do not guess” text; (3) the final answer is followed by a deterministic Evidence table generated from real
  ToolResults; (4) tests with a fake LLM verify facts only come from tool data.
- **Status:** Accepted (2026-11-10)

## D17 — Structured logging with stdlib `logging` + JSON formatter and secret redaction
- **Decision:** One JSON line per tool call: `ts, level, request_id, tool, status, duration_ms`; redaction filter
  masks values of keys/patterns like `key`, `token`, `password`, `Authorization`, `sk-ant-…`.
- **Status:** Accepted (2026-11-10)

## D18 — Retraining recommendation policy
- **Decision:** `config/agent_policy.yaml`: `max_model_age_days: 30`, newer dataset version than champion's,
  last pipeline run failed at quality gate, or supplied degradation metrics below `min_f1`. Otherwise the agent
  states retraining is not indicated.
- **Status:** Accepted (2026-11-10)

## D19 — User interface: CLI (with `--once` mode and transcript export)
- **Alternatives:** Streamlit/Gradio chat UI, FastAPI chat endpoint.
- **Rationale:** Lowest complexity; the confirmation prompt is natural in a terminal; transcripts become report evidence.
- **Status:** Accepted (2026-11-10)

## D20 — CI: GitHub Actions with uv, ruff, pytest (all externals mocked)
- **Decision:** Trigger on `pull_request` and `push` to `main`; jobs: lint (`ruff check`, `ruff format --check`),
  tests (`pytest -m "not integration"`), manifest schema validation.
- **Status:** Accepted (2026-11-10)

## D21 — Injected failures
- **Decision:** F1 = misconfigured ConfigMap `MODEL_PATH` (readiness failure, no endpoints);
  F2 = corrupt dataset version failing `validate_data`; F3 (optional) = MLflow server down.
- **Rationale:** Cover infrastructure, data/workflow, and service-availability layers as the learning outcomes require.
- **Status:** Accepted (2026-11-10)

## D22 — GitHub repository
- **Decision:** Public repo named `assignment-03-mlops-agent`; user creates it (no `gh` CLI here) and gives me
  the remote URL. Default branch `main`, feature work via PR at least once to prove CI on pull requests.
- **Status:** Accepted (2026-11-10)

## D23 — Secrets handling
- **Decision:** `.env` (gitignored) + committed `.env.example` with placeholders; settings via pydantic-settings;
  kubeconfig stays in `~/.kube`; pre-push secret scan (e.g. `gitleaks` or a simple regex check) before submission.
- **Status:** Accepted (2026-11-10)

## D24 — Inference image contents & versioning
- **Decision:** `python:3.12-slim`, non-root, model files baked in at build time (exported from MLflow
  `champion`). Version exposed via image tag, pod label `app.kubernetes.io/version`, ConfigMap `APP_VERSION`,
  and `/version` endpoint (also returns MLflow model version + dataset version).
- **Rationale:** Makes “which version is deployed?” answerable from several independent sources.
- **Status:** Accepted (2026-11-10)

## D25 — LLM: Google Gemini (free tier, Google AI Studio key) via the official `google-genai` SDK
- **Context:** User wants a free model; will supply a Google AI Studio API key when the agent step (S13) is reached.
- **Decision:** Use Gemini function calling through the `google-genai` Python SDK. Model configurable via
  `AGENT_MODEL` (default: a current free-tier Flash model, confirmed against the live model list when the key is
  available); key read from `GEMINI_API_KEY` in `.env` (gitignored, never committed, never logged).
  The LLM sits behind an `LLMClient` interface (D3), so tests use a scripted fake and CI needs no key.
- **Free-tier constraints handled:** low requests-per-minute → retry with exponential backoff on HTTP 429;
  bounded number of loop iterations; compact tool outputs to stay within token limits.
- **Alternatives:** Anthropic Claude (paid, D2), local Ollama (free but weak tool calling, heavy on CPU).
- **Status:** Accepted (2026-11-10)

## D26 — Local Kubernetes confirmed as kind
- **Context:** User delegated D11.
- **Decision:** Keep kind (D11). Docker Desktop Kubernetes is the documented fallback if kind cannot be installed.
- **Rationale:** Cluster creation is scripted and repeatable (`kind create cluster --config kubernetes/kind-config.yaml`),
  images load without a registry, and the cluster can be deleted and rebuilt identically for the demo.
- **Status:** Accepted (2026-11-10)

## D27 — Batch verification instead of per-step stops (from S2 onward)
- **Context:** After approving S1 the user asked Claude to continue through all remaining steps without stopping,
  and to stop only when something from the user is needed (e.g. the Gemini API key).
- **Decision:** Steps S2..S18 are executed back-to-back; each step is committed and pushed separately
  (`S<n>: …`) and marked `AWAITING VERIFICATION` in PLAN.md so the user can verify them in one pass later.
- **Status:** Accepted (2026-11-13) — supersedes the "stop after every step" rule of D0 for S2..S18.

## D28 — GitHub remote and commit authorship
- **Decision:** Remote `origin` = `https://github.com/Myt1x54/assignment-03-mlops-agent.git`, branch `main`.
  Commits are authored by the student only — no co-author/contributor trailers for the assistant.
- **Status:** Accepted (2026-11-13) — supersedes the "user creates repo" part of D22.
