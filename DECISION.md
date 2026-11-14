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

## D29 — MLflow model serialization: skops with explicit trusted types
- **Context:** MLflow 3 logs sklearn models in the skops format by default (no arbitrary-code pickle). Tree-based
  models (random forest, gradient boosting) contain `sklearn.tree._tree.Tree`, which skops refuses to load unless
  it is explicitly trusted, so `log_model` failed for those models.
- **Decision:** `training/evaluate.py::trusted_types()` asks skops which types it flags in the freshly trained
  pipeline and passes exactly those as `skops_trusted_types`. We do **not** fall back to pickle/cloudpickle.
- **Rationale:** Keeps the safer format; the trusted list is computed from a model this same process just built from
  scikit-learn classes, so nothing foreign is trusted. `skops` is pinned in `infra/ml-requirements.txt`.
- **Status:** Accepted (2026-11-14)

## D30 — DVC remote path is stored relative to `.dvc/`
- **Context:** `dvc remote add` with a relative path rewrites it relative to `.dvc/config`; the first push landed
  one directory too high.
- **Decision:** Remote `localremote` = `../../mlops-dvc-remote` in `.dvc/config`, which resolves to a sibling folder
  of the repo (`<parent>/mlops-dvc-remote`). README documents re-creating it with `dvc remote modify`.
- **Status:** Accepted (2026-11-14)

## D31 — kind cluster config lives in `infra/`
- **Context:** With `kind-config.yaml` inside `kubernetes/`, `kubectl apply -f kubernetes/` tried to apply the kind
  `Cluster` object and failed.
- **Decision:** Move it to `infra/kind-config.yaml` (cluster tooling sits next to docker compose). `kubernetes/` holds
  only objects the cluster can apply. The path in D26 should be read as `infra/kind-config.yaml`.
- **Status:** Accepted (2026-11-14)

## D32 — Champion/challenger promotion rule
- **Context:** The first Airflow run promoted a logistic-regression model (F1 0.8026) over the seeded champion v7
  (gradient boosting, F1 0.8121) simply because it passed the absolute quality gate.
- **Decision:** `promote_model` moves alias `champion` only when the candidate passes the gate **and** has a strictly
  higher F1 than the current champion. Otherwise it keeps the champion, tags the run `promoted=false` with a
  `promotion.reason`, tags the model version `promotion=skipped: not better than champion`, and exits 0 (a normal
  outcome, not a task failure). The seeding script uses `force=True` to set the initial champion.
- **Rationale:** Prevents regressions in production; the recorded reason lets the agent answer "why was this model not
  promoted?" from tool evidence. The one wrong promotion (v12) was reverted and tagged with the reason.
- **Status:** Accepted (2026-11-14)

## D33 — Confirmation tokens bound to the exact action; tool inputs live next to tools
- **Context:** S8 needs a confirmation workflow the LLM cannot satisfy on its own, and a home for per-tool inputs.
- **Decision:**
  - A state-changing tool first returns `confirmation_required` with a summary and an HMAC-signed token
    (per-process random key) over `{tool, args, issued_at}`; tokens expire after
    `safety.confirmation_ttl_seconds` (300 s). The token is stripped from what the LLM sees (`ToolResult.for_llm`)
    and kept by the orchestrator, which re-executes only after a human types "yes".
  - All tool input models inherit `ToolInput` (`extra="forbid"`), so an LLM-supplied `confirmed=true` is a
    validation error, never an approval.
  - Per-tool input models are defined in the tool module that uses them (`agent/tools/<system>_tools.py`);
    `agent/schemas.py` keeps only shared contracts. Supersedes the "per-tool input models in schemas.py" wording
    of PLAN S8 task 1.
- **Rationale:** An approval for "restart churn-api" cannot be replayed for another deployment, namespace or replica
  count, and cannot be forged by the model.
- **Status:** Accepted (2026-11-14)

## D34 — Triggering the training DAG also needs human confirmation
- **Context:** AGENT.md marks `airflow_trigger_dag` as **W** (approved DAG only). A training run costs compute,
  writes new model versions and may promote a champion, and the mandatory scenario requires that analysis
  questions never trigger it.
- **Decision:** `airflow_trigger_dag` is **W C**: DAG allow-list check first (non-approved → `rejected`), then the
  same orchestrator-held confirmation token as the Kubernetes write tools. Optional `data_path` must be a `.csv`
  under `data/` (no `..`), passed as DAG conf.
- **Rationale:** Defense in depth: even if the model misreads intent, nothing runs without a human "yes".
- **Status:** Accepted (2026-11-14)

## D35 — Default LLM: `gemini-3.5-flash`
- **Context:** With the user's AI Studio key, `gemini-flash-latest` returned 404 for `generateContent`, the
  2.5-generation models are closed to new users, and `gemini-3.8-flash` timed out (HTTP 504 after ~58 s) in a probe.
  `gemini-3.5-flash` answered a tool-calling probe correctly in ~1.3 s.
- **Decision:** Default `AGENT_MODEL=gemini-3.5-flash` (overridable in `.env`). The Gemini client now has a 60 s HTTP
  timeout so a slow model fails (5xx/timeouts are retried with backoff) instead of hanging the CLI. Supersedes the
  model choice in D25 (the provider choice stays).
- **Status:** Accepted (2026-11-14)

## D36 — Free-tier resilience: honour retryDelay, fall back across models on daily quota
- **Context:** During the demos the free tier returned `429 RESOURCE_EXHAUSTED`, both as a per-minute limit and
  as a per-day quota (`GenerateRequestsPerDayPerProjectPerModel-FreeTier`) whose `retryDelay` is many hours.
- **Decision:** `GeminiClient` parses the error body. Per-minute limits wait the server's `retryDelay` (capped at
  65 s) or exponential backoff. An exhausted daily quota switches immediately to the next model in
  `AGENT_FALLBACK_MODELS` (default `gemini-3.5-flash-lite,gemini-3.1-flash-lite`). After a switch the history is
  rebuilt without provider-native parts, because thought signatures belong to one model.
- **Rationale:** Keeps the agent usable on a free key without hiding failures. If every model is exhausted the
  orchestrator reports `llm_error` honestly instead of inventing an answer.
- **Status:** Accepted (2026-11-14)

## D37 — Deterministic intent guard against target substitution
- **Context (unexpected failure found in the live demo):** The user asked to "restart the churn-api deployment in the
  default namespace". `gemini-3.5-flash-lite` called `k8s_restart_deployment` with namespace **`mlops-demo`**
  instead. It silently replaced the user's target with the allowed one, so the call reached the confirmation
  prompt instead of being rejected (transcript `unexpected-llm-namespace-substitution.md`). The human gate caught
  it because the panel shows the real target, and a new prompt rule (#9) did not change the model's behaviour.
- **Decision:** The orchestrator compares every state-changing call with the user's message. If the message names
  a namespace (`namespace X`, `X namespace`, `-n X`) and the call targets a different one, the call is rejected
  with `IntentMismatch` before any confirmation or API call. Read-only tools are not affected.
- **Rationale:** Safety must not depend on the LLM following instructions. Policy is enforced in code (allow-list),
  intent in code (this guard) and consent by the human (confirmation).
- **Status:** Accepted (2026-11-14)
