# Architecture

One conversational agent operates one ML application: a customer-churn classifier, from versioned data to a
serving deployment. The agent can **only** learn facts through typed tools. Every tool result carries an evidence
id, and the final answer ends with an evidence table generated from those results (not from LLM text).

## 1. System overview

```mermaid
flowchart LR
    user([Operator]) -- question / yes-no --> cli[CLI<br/>agent/main.py]
    cli --> orch[Orchestrator<br/>agent/orchestrator.py]
    orch <-- function calling --> llm[(Gemini<br/>google-genai)]
    orch --> reg[Tool registry<br/>validate · permission · timeout · log]
    reg --> dvc[DVC tools<br/>git + dvc subprocess<br/>allow-listed]
    reg --> mlf[MLflow tools<br/>MlflowClient]
    reg --> afl[Airflow tools<br/>REST API v2 + JWT]
    reg --> k8s[Kubernetes tools<br/>official python client]

    subgraph local[Local platform: docker compose + kind]
        repo[(Git repo + DVC cache<br/>data-v1.0 / data-v2.0)]
        mlflow[(MLflow server :5000<br/>runs + model registry)]
        airflow[Airflow 3 :8080<br/>ml_training_pipeline]
        cluster[kind cluster mlops<br/>ns mlops-demo<br/>churn-api x2]
    end
    dvc --> repo
    mlf --> mlflow
    afl --> airflow
    k8s --> cluster
    airflow -- train / log / gate / promote --> mlflow
    airflow -- reads data --> repo
    mlflow -- export champion --> image[mlops-api image] --> cluster
```

## 2. ML lifecycle and provenance chain

```mermaid
flowchart LR
    A[make_dataset.py<br/>v1 / v2 / corrupt] --> B[dvc add + git tag<br/>data-v1.0, data-v2.0]
    B --> C[validate_data]
    C --> D[preprocess] --> E[train] --> F[evaluate_and_log<br/>MLflow run + registry version<br/>alias candidate]
    F --> G{quality_gate<br/>F1 ≥ 0.70, AUC ≥ 0.80}
    G -- pass --> H{promote_model<br/>beats champion F1?}
    G -- fail --> X[run + version tagged quality_gate=failed<br/>downstream upstream_failed]
    H -- yes --> I[alias champion]
    H -- no --> Y[promoted=false + reason]
    I --> J[export_model.py → model/] --> K[docker build mlops-api] --> L[kind load + rollout]
```

Every MLflow run is tagged with `data.version`, `data.dvc_md5`, `data.file_md5`, `data.in_sync`, `git.commit`,
`dvc.remote` and `pipeline.run_id`. The serving container reports the model version, MLflow run id and dataset
version on `GET /version`. This is how the agent links **deployment → model → run → dataset** using evidence alone:
`k8s_get_deployed_version` → `mlflow_get_model_versions` → `dvc_get_run_dataset` (it re-checks the md5 against
the git tag).

The same stages exist twice:
- `dvc.yaml` (`dvc repro`) for local reproducibility.
- `dags/training_pipeline.py` for orchestrated runs, with retries and an isolated `/opt/ml-venv`.

## 3. Request flow inside the agent

```mermaid
sequenceDiagram
    participant U as Operator
    participant O as Orchestrator
    participant L as Gemini
    participant R as Tool registry
    participant S as Backend
    U->>O: question
    O->>L: system prompt + history + 27 tool schemas
    L-->>O: function calls (possibly several)
    par independent calls run concurrently
        O->>R: execute_tool(name, args, ctx)
        R->>R: Pydantic validate (extra=forbid)
        R->>R: allow-lists (namespace, DAG, command, limits)
        R->>S: call with timeout
        S-->>R: data or failure
        R-->>O: ToolResult {status, data, error, duration_ms}
    end
    alt status = confirmation_required
        O->>U: exact action summary - type 'yes'?
        U-->>O: yes / no
        O->>R: re-execute with HMAC token bound to tool+args (never shown to the LLM)
    end
    O->>L: tool results (evidence ids E1..En)
    L-->>O: final answer (Facts / Uncertainties / Recommendations)
    O->>O: append "Not verified" note, grounding check, evidence table
    O-->>U: answer + evidence table (+ transcript file)
```

## 4. Tool catalogue and permissions (27 tools)

R = read-only. W C = changes state and needs human confirmation collected by the orchestrator.

| System | Tool | Perm | Guard rails |
|---|---|---|---|
| DVC | `dvc_list_tracked_datasets` | R | reads `.dvc` / `dvc.lock` files |
| DVC | `dvc_get_current_version` | R | allow-listed `git` |
| DVC | `dvc_list_dataset_versions` | R | allow-listed `git` |
| DVC | `dvc_compare_versions` | R | `dvc diff`; revisions validated (no options, no ranges) |
| DVC | `dvc_detect_changes` | R | `dvc status`, `git status` |
| DVC | `dvc_get_run_dataset` | R | MLflow tags re-verified against git tag md5 |
| DVC | `dvc_reproduce_instructions` | R | optional checkout only into an isolated scratch `git worktree` |
| MLflow | `mlflow_list_experiments` | R | `MlflowClient`, retries capped, 10 s HTTP timeout |
| MLflow | `mlflow_list_runs` | R | |
| MLflow | `mlflow_get_run` | R | |
| MLflow | `mlflow_best_run` | R | filters: dataset version, gate passed, recency |
| MLflow | `mlflow_compare_runs` | R | 2..10 validated run ids |
| MLflow | `mlflow_get_model_versions` | R | aliases champion / candidate |
| Airflow | `airflow_list_dags` | R | JWT, cached and refreshed on 401 |
| Airflow | `airflow_list_dag_runs` | R | |
| Airflow | `airflow_get_task_instances` | R | first failed task, upstream_failed list |
| Airflow | `airflow_get_task_logs` | R | tail clamped (≤ 200 lines), redacted |
| Airflow | `airflow_trigger_dag` | W C | approved DAG list; `data_path` must be `data/**.csv` |
| K8s | `k8s_list_deployments` | R | namespace allow-list on **every** tool |
| K8s | `k8s_list_pods` | R | |
| K8s | `k8s_get_pod_logs` | R | scans ≤ 2000 lines for errors, returns a short tail |
| K8s | `k8s_get_events` | R | |
| K8s | `k8s_get_services` | R | ready endpoints via EndpointSlices |
| K8s | `k8s_find_unhealthy` | R | not-Ready / CrashLoop / restarts / stalled rollout / no endpoints |
| K8s | `k8s_get_deployed_version` | R | image, labels, ConfigMap, live `/version` via API-server proxy |
| K8s | `k8s_restart_deployment` | W C | `mlops-demo` only |
| K8s | `k8s_scale_deployment` | W C | `mlops-demo` only, replicas 1..5 |

The cluster side mirrors these rules. The `mlops-agent` ServiceAccount (`kubernetes/rbac.yaml`) can read in
`mlops-demo` and only `patch` deployments and their scale subresource. It has no Secrets, no delete or create,
and no other namespaces.

## 5. Failure handling contract

| Situation | Tool status | What the user sees |
|---|---|---|
| Backend unreachable (connection refused, DNS, kubeconfig missing) | `unavailable` | "Not verified" note naming the system; LLM told not to fill the gap |
| Tool exceeded its timeout | `timeout` | same as above |
| Bad arguments from the LLM | `error` (`ValidationError`) | LLM sees the validation message and can correct itself |
| Policy violation (namespace, DAG, command, replicas) | `rejected` | refusal with the rule that was violated; no API call made |
| Write tool without approval | `confirmation_required` → human prompt | nothing executes unless the human types `yes` |
| LLM API down / quota | orchestrator `stopped_reason=llm_error` | honest error, no answer invented |
| Figures in the answer not present in any tool result | — | "Grounding check" warning listing them |

## 6. Deployment topology

| Component | Where | Port | Notes |
|---|---|---|---|
| MLflow 3.18 | docker compose (`infra/docker-compose.yml`) | 5000 | sqlite backend, proxied artifact store (`--serve-artifacts`) |
| Airflow 3.1 | docker compose: api-server, scheduler, dag-processor, Postgres 16 | 8080 | LocalExecutor, FAB auth, REST API v2 |
| churn-api | kind cluster `mlops`, ns `mlops-demo` | Service :80 → 8000 | 2 replicas, RollingUpdate `maxUnavailable: 0`, probes, requests/limits, ConfigMap, non-root, read-only rootfs |
| Agent | host (`uv run python -m agent.main`) | — | Python 3.12, kubeconfig context `kind-mlops` |
