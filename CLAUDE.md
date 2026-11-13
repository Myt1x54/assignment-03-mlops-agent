# CLAUDE.md — Working Guide for Claude Code

This repository is **Assignment 03: Building a Production-Ready MLOps AI Agent** (MLOps & Production AI
Engineering, Fall 2026). The only thing submitted is the **URL of the GitHub repository**, so everything the
grader needs (code, infra, tests, evidence, report) must live in this repo.

Read these files before doing any work:

| File | Purpose |
|---|---|
| `INSTRUCTION.md` | Condensed assignment requirements, rubric checklist, and local prerequisites |
| `PLAN.md` | Ordered implementation steps `S0..Sn` with acceptance criteria and status |
| `DECISION.md` | Architecture/technology decisions `D0..Dn` with rationale |
| `AGENT.md` | Specification of the runtime MLOps AI agent (tools, permissions, reasoning rules) |

---

## 1. Working protocol (mandatory)

1. Work on **exactly one PLAN step at a time**, in order, unless the user says otherwise.
2. Before starting a step, set its status in `PLAN.md` to `IN PROGRESS`.
3. When a step is finished:
   - Set its status to `AWAITING VERIFICATION`.
   - Explain to the user, in plain language: what was built, why, which files changed, how to check it
     (exact commands), and any decisions taken.
   - **Stop and ask the user to verify.** Do not begin the next step until the user confirms.
     *(Exception — D27: for S2..S18 the user asked for continuous execution with one batched verification at
     the end; stop only when user input is required.)*
4. After user approval, set the step to `DONE (verified YYYY-MM-DD)`.
5. Every non-trivial technical choice gets an entry in `DECISION.md` (`D<n>`, next free number). Never
   renumber or delete old decisions; supersede them with a new entry (`Status: Superseded by D<m>`).
6. If a step reveals that the plan is wrong, propose a PLAN change to the user instead of silently deviating.
7. Commit and push to `origin/main` at the end of each step (D27, D28), message format:
   `S<n>: <short summary>`. **No `Co-Authored-By` or other assistant attribution lines** (user instruction).

## 2. Environment facts (Windows 11 dev machine)

- Shell: PowerShell 5.1 (primary) and Git Bash. Use forward slashes in Python paths.
- Python for the project: **3.12 via `uv`** (system has 3.14, which Airflow/MLflow do not fully support).
- Docker Desktop must be running for MLflow, Airflow, image builds, and kind.
- `kubectl` is installed; a local cluster (kind) must be created — see `INSTRUCTION.md`.
- GitHub remote: `origin` → https://github.com/Myt1x54/assignment-03-mlops-agent.git (`gh` CLI not installed; push via git).
- The Bash tool sandbox cannot download from files.pythonhosted.org; run `uv sync` / `uv add` from PowerShell.

## 3. Common commands (valid once S1 is complete)

```bash
uv sync                          # install locked dependencies
uv run ruff check .              # lint
uv run ruff format --check .     # format check
uv run pytest -q                 # tests (all external services mocked)
uv run python -m agent.main      # start the conversational agent (CLI)
docker compose -f infra/docker-compose.yml up -d   # MLflow + Airflow
```

## 4. Coding conventions

- Package layout follows the assignment's recommended structure (`agent/`, `api/`, `dags/`, `training/`,
  `kubernetes/`, `tests/`, ...). Do not rename top-level folders.
- Every agent tool:
  - lives in `agent/tools/<system>_tools.py`,
  - has a Pydantic input model and returns the common `ToolResult` envelope from `agent/schemas.py`,
  - never raises to the orchestrator — failures become `status="error" | "timeout" | "unavailable"`,
  - has a timeout, structured log line, and at least one unit test with mocked backends.
- Type hints everywhere; `ruff` must pass with zero warnings.
- No real network/cluster calls in tests; use mocks/fakes. Integration tests that need live services are
  marked `@pytest.mark.integration` and skipped in CI.

## 5. Safety rules (enforced in code, not just prompts)

- **Never commit secrets**: API keys, kubeconfigs, tokens, passwords. Use `.env` (gitignored) and
  `.env.example` with placeholders only.
- Kubernetes state-changing operations: only in the approved namespace (`mlops-demo`) and only after explicit
  human confirmation handled by the orchestrator — the LLM can never confirm on its own.
- Airflow: only DAG IDs in the approved list may be triggered.
- DVC/Git: only allow-listed read-only subcommands; dataset checkout only into an isolated scratch worktree.
- The agent must never state an operational fact that is not backed by a tool result in the current request.

## 6. Definition of done for the whole project

All rubric items in `INSTRUCTION.md` are checked, CI is green on a pull request, the five demo scenarios and
two failure investigations are captured in `docs/report/`, and the README lets a stranger reproduce everything.
