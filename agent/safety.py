"""Safety layer enforced in code (CLAUDE.md §5, DECISIONS D12, D13).

* Allow-lists: Kubernetes namespaces, approved Airflow DAG ids, DVC/git subcommands.
* Limits: replica range, log-tail size.
* Confirmation workflow for state-changing tools: the first call raises ``ConfirmationRequiredError`` with a
  human-readable summary and a token bound to *exactly* this tool + these arguments. Only the orchestrator,
  after a human types "yes", re-executes the tool with that token. The LLM never sees a way to confirm: tool
  input models forbid extra fields, and tokens are HMAC-signed with a per-process secret.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Sequence
from typing import Any

from agent.errors import ConfirmationRequiredError, PermissionDeniedError

_PROCESS_KEY = secrets.token_bytes(32)

# (program, subcommand...) prefixes that tools may run. Everything is read-only.
ALLOWED_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("dvc", "status"),
    ("dvc", "diff"),
    ("dvc", "data", "status"),
    ("dvc", "list"),
    ("dvc", "params", "diff"),
    ("git", "log"),
    ("git", "show"),
    ("git", "tag"),
    ("git", "rev-parse"),
    ("git", "status"),
    ("git", "worktree", "add"),  # only used for isolated dataset checkout into a scratch dir
)
_FORBIDDEN_TOKENS = {"&&", "||", ";", "|", ">", "<", "`", "$("}


def check_namespace(namespace: str, allowed: Sequence[str]) -> None:
    if namespace not in allowed:
        raise PermissionDeniedError(
            f"namespace '{namespace}' is not allowed; permitted namespaces: {', '.join(allowed) or 'none'}"
        )


def check_dag(dag_id: str, approved: Sequence[str]) -> None:
    if dag_id not in approved:
        raise PermissionDeniedError(
            f"DAG '{dag_id}' is not on the approved list ({', '.join(approved) or 'none'}); "
            "refusing to trigger"
        )


def check_replicas(replicas: int, minimum: int, maximum: int) -> None:
    if not minimum <= replicas <= maximum:
        raise PermissionDeniedError(f"replicas={replicas} outside the allowed range {minimum}..{maximum}")


def check_command(argv: Sequence[str]) -> None:
    """Allow only exact allow-listed command prefixes, with no shell metacharacters anywhere."""
    if any(tok in arg for arg in argv for tok in _FORBIDDEN_TOKENS):
        raise PermissionDeniedError(f"shell metacharacters are not allowed: {' '.join(argv)}")
    for prefix in ALLOWED_COMMANDS:
        if tuple(argv[: len(prefix)]) == prefix:
            return
    raise PermissionDeniedError(f"command not on the read-only allow-list: {' '.join(argv[:3])}")


def clamp_tail(lines: int, maximum: int) -> int:
    return max(1, min(lines, maximum))


# --- confirmation tokens -------------------------------------------------------------------------------


def _digest(tool: str, args: dict[str, Any], issued: int) -> str:
    message = json.dumps({"tool": tool, "args": args, "issued": issued}, sort_keys=True, default=str)
    return hmac.new(_PROCESS_KEY, message.encode(), hashlib.sha256).hexdigest()[:32]


def issue_token(tool: str, args: dict[str, Any]) -> str:
    issued = int(time.time())
    return f"{issued}.{_digest(tool, args, issued)}"


def verify_token(token: str | None, tool: str, args: dict[str, Any], ttl_seconds: int) -> bool:
    if not token or "." not in token:
        return False
    issued_text, digest = token.split(".", 1)
    if not issued_text.isdigit():
        return False
    issued = int(issued_text)
    if time.time() - issued > ttl_seconds:
        return False
    return hmac.compare_digest(digest, _digest(tool, args, issued))


def require_confirmation(
    *, tool: str, args: dict[str, Any], token: str | None, ttl_seconds: int, summary: str
) -> None:
    """Raise ``ConfirmationRequiredError`` unless ``token`` is a valid human approval for this action."""
    if verify_token(token, tool, args, ttl_seconds):
        return
    raise ConfirmationRequiredError(
        summary,
        action={"tool": tool, "args": args, "confirmation_token": issue_token(tool, args)},
    )
