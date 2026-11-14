"""DVC / data-provenance tools (AGENT.md §3.1, DECISION D14).

All subprocess calls go through ``run_cmd``: allow-listed read-only commands only, ``shell=False``,
bounded by a timeout, executed in the repository root. ``dvc`` runs as ``python -m dvc``
(no PATH lookup).
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, field_validator

from agent.errors import InvalidRequestError, ServiceUnavailableError
from agent.safety import check_command
from agent.schemas import NoInput, ToolInput
from agent.tools.base import ToolContext, tool

SYSTEM = "dvc"
DEFAULT_DATASET = "data/raw/churn.csv"
TAG_PATTERN = "data-*"
_REV_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,99}$")
_PATH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./\-]{0,199}$")
SEP = "\x1f"  # git format field separator (%x1f); avoids shell-looking characters in argv


# --- helpers ------------------------------------------------------------------------------------------------


def run_cmd(argv: list[str], ctx: ToolContext, timeout: float | None = None) -> str:
    """Run an allow-listed command and return stdout. Raises typed failures, never CalledProcessError."""
    check_command(argv)
    real = [sys.executable, "-m", "dvc", *argv[1:]] if argv[0] == "dvc" else argv
    try:
        proc = subprocess.run(  # noqa: S603 - argv is allow-listed above, shell=False
            real,
            cwd=ctx.settings.repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout or ctx.settings.agent_tool_timeout_seconds,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ServiceUnavailableError(f"'{argv[0]}' executable not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"{' '.join(argv[:3])} timed out") from exc
    if proc.returncode != 0:
        raise InvalidRequestError(f"{' '.join(argv[:3])} failed: {proc.stderr.strip()[:500]}")
    return proc.stdout


def pointer_info(text: str) -> dict[str, Any]:
    out = (yaml.safe_load(text) or {}).get("outs", [{}])[0]
    return {"md5": out.get("md5"), "size": out.get("size"), "path": out.get("path")}


def pointer_at(rev: str, dataset: str, ctx: ToolContext) -> dict[str, Any]:
    return pointer_info(run_cmd(["git", "show", f"{rev}:{dataset}.dvc"], ctx))


def data_tags(ctx: ToolContext) -> list[dict[str, str]]:
    """Dataset tags, newest first, with date and annotation subject."""
    out = run_cmd(
        [
            "git",
            "tag",
            "--list",
            TAG_PATTERN,
            "--sort=-creatordate",
            "--format=%(refname:short)%1f%(creatordate:iso-strict)%1f%(subject)",
        ],
        ctx,
    )
    tags = []
    for line in out.splitlines():
        if line.strip():
            name, date, subject = [*line.split(SEP, 2), "", ""][:3]
            tags.append({"tag": name, "date": date, "message": subject})
    return tags


def version_for_md5(md5: str | None, dataset: str, ctx: ToolContext) -> str | None:
    if not md5:
        return None
    for tag in data_tags(ctx):
        try:
            if pointer_at(tag["tag"], dataset, ctx)["md5"] == md5:
                return tag["tag"]
        except InvalidRequestError:
            continue
    return None


def file_md5(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.md5()  # noqa: S324 - DVC content hash, not security
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class DatasetIn(ToolInput):
    dataset: str = Field(default=DEFAULT_DATASET, description="DVC-tracked data path relative to repo root")

    @field_validator("dataset")
    @classmethod
    def _safe_path(cls, v: str) -> str:
        if ".." in v or not _PATH_RE.match(v):
            raise ValueError("dataset must be a relative path inside the repository")
        return v


def _rev(v: str) -> str:
    if not _REV_RE.match(v) or ".." in v:
        raise ValueError("revision must be a git tag, branch or commit (no options or ranges)")
    return v


# --- tools --------------------------------------------------------------------------------------------------


@tool(
    name="dvc_list_tracked_datasets",
    system=SYSTEM,
    description="List data tracked by DVC (.dvc pointers and dvc.lock pipeline outputs) with md5 and size.",
    input_model=NoInput,
)
def dvc_list_tracked_datasets(args: NoInput, ctx: ToolContext) -> dict[str, Any]:
    root = ctx.settings.repo_root
    pointers = []
    for p in sorted(root.rglob("*.dvc")):
        rel = p.relative_to(root)
        if rel.parts[0] in {".dvc", ".venv", "node_modules"} or not p.is_file():
            continue
        info = pointer_info(p.read_text(encoding="utf-8"))
        pointers.append({"pointer": rel.as_posix(), "data_path": rel.with_suffix("").as_posix(), **info})
    stage_outs = []
    lock = root / "dvc.lock"
    if lock.exists():
        for stage, body in (yaml.safe_load(lock.read_text(encoding="utf-8")) or {}).get("stages", {}).items():
            for out in body.get("outs", []):
                stage_outs.append(
                    {"stage": stage, "path": out.get("path"), "md5": out.get("md5"), "size": out.get("size")}
                )
    return {"tracked_files": pointers, "pipeline_outputs": stage_outs}


@tool(
    name="dvc_get_current_version",
    system=SYSTEM,
    description="Current dataset version in the workspace: git commit, data tag, DVC md5, in-sync flag.",
    input_model=DatasetIn,
)
def dvc_get_current_version(args: DatasetIn, ctx: ToolContext) -> dict[str, Any]:
    root = ctx.settings.repo_root
    pointer_path = root / f"{args.dataset}.dvc"
    if not pointer_path.exists():
        raise InvalidRequestError(f"{args.dataset} is not tracked by DVC (no {args.dataset}.dvc)")
    info = pointer_info(pointer_path.read_text(encoding="utf-8"))
    actual = file_md5(root / args.dataset)
    return {
        "dataset": args.dataset,
        "git_commit": run_cmd(["git", "rev-parse", "HEAD"], ctx).strip(),
        "data_version_tag": version_for_md5(info["md5"], args.dataset, ctx) or "untagged",
        "dvc_md5": info["md5"],
        "size_bytes": info["size"],
        "workspace_file_md5": actual or "missing",
        "workspace_in_sync": actual == info["md5"],
    }


@tool(
    name="dvc_list_dataset_versions",
    system=SYSTEM,
    description="List dataset versions: data-* git tags (md5/size) and commits that changed the pointer.",
    input_model=DatasetIn,
)
def dvc_list_dataset_versions(args: DatasetIn, ctx: ToolContext) -> dict[str, Any]:
    versions = []
    for tag in data_tags(ctx):
        try:
            info = pointer_at(tag["tag"], args.dataset, ctx)
        except InvalidRequestError:
            continue
        versions.append({**tag, "md5": info["md5"], "size_bytes": info["size"]})
    pointer = f"{args.dataset}.dvc"
    log = run_cmd(
        ["git", "log", "--date=iso-strict", "--format=%H%x1f%ad%x1f%s", "-n", "20", "--", pointer],
        ctx,
    )
    fields = ("commit", "date", "subject")
    commits = [dict(zip(fields, line.split(SEP, 2), strict=False)) for line in log.splitlines() if line]
    return {
        "dataset": args.dataset,
        "tagged_versions": versions,
        "latest_version": versions[0]["tag"] if versions else None,
        "pointer_commits": commits,
    }


class CompareIn(DatasetIn):
    rev_a: str = Field(description="Older revision, e.g. 'data-v1.0'")
    rev_b: str = Field(default="HEAD", description="Newer revision, e.g. 'data-v2.0' or 'HEAD'")

    @field_validator("rev_a", "rev_b")
    @classmethod
    def _check_revs(cls, v: str) -> str:
        return _rev(v)


@tool(
    name="dvc_compare_versions",
    system=SYSTEM,
    description="Compare two dataset revisions: 'dvc diff' plus md5/size of the dataset at each revision.",
    input_model=CompareIn,
)
def dvc_compare_versions(args: CompareIn, ctx: ToolContext) -> dict[str, Any]:
    raw = run_cmd(["dvc", "diff", "--json", args.rev_a, args.rev_b], ctx, timeout=60)
    diff = json.loads(raw) if raw.strip() else {}
    a = pointer_at(args.rev_a, args.dataset, ctx)
    b = pointer_at(args.rev_b, args.dataset, ctx)
    return {
        "rev_a": args.rev_a,
        "rev_b": args.rev_b,
        "dvc_diff": {k: [str(e.get("path")).replace("\\", "/") for e in v] for k, v in diff.items() if v},
        "dataset": args.dataset,
        "a": a,
        "b": b,
        "dataset_changed": a["md5"] != b["md5"],
        "size_delta_bytes": (b["size"] or 0) - (a["size"] or 0),
    }


@tool(
    name="dvc_detect_changes",
    system=SYSTEM,
    description="Is the workspace in sync? 'dvc status' of pipeline stages + git status of .dvc pointers.",
    input_model=NoInput,
)
def dvc_detect_changes(args: NoInput, ctx: ToolContext) -> dict[str, Any]:
    raw = run_cmd(["dvc", "status", "--json"], ctx, timeout=60)
    status = json.loads(raw) if raw.strip() else {}
    porcelain = run_cmd(["git", "status", "--porcelain", "--", "*.dvc", "dvc.lock", "params.yaml"], ctx)
    return {
        "in_sync": not status and not porcelain.strip(),
        "stale_stages": sorted(status),
        "dvc_status": status,
        "uncommitted_pointer_changes": [line.strip() for line in porcelain.splitlines() if line.strip()],
    }


class RunIn(ToolInput):
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$", description="MLflow run id (32 hex chars)")


@tool(
    name="dvc_get_run_dataset",
    system=SYSTEM,
    description=(
        "Resolve which dataset version trained an MLflow run: reads the run's provenance tags and "
        "verifies the recorded DVC md5 against the git tag in this repository."
    ),
    input_model=RunIn,
)
def dvc_get_run_dataset(args: RunIn, ctx: ToolContext) -> dict[str, Any]:
    from agent.tools.mlflow_tools import get_run_tags

    tags = get_run_tags(args.run_id, ctx)
    recorded_version = tags.get("data.version")
    recorded_md5 = tags.get("data.dvc_md5")
    dataset = tags.get("data.path", DEFAULT_DATASET)
    verified: bool | None = None
    tag_md5 = None
    if recorded_version and recorded_version.startswith("data-"):
        try:
            tag_md5 = pointer_at(recorded_version, dataset, ctx)["md5"]
            verified = tag_md5 == recorded_md5
        except InvalidRequestError:
            verified = False
    return {
        "run_id": args.run_id,
        "data_version": recorded_version or "unknown",
        "dvc_md5_recorded": recorded_md5 or "unknown",
        "dvc_md5_at_tag": tag_md5,
        "verified_against_git": verified,
        "data_path": dataset,
        "git_commit": tags.get("git.commit"),
        "data_in_sync_at_training": tags.get("data.in_sync"),
        "dvc_remote": tags.get("dvc.remote"),
        "resolved_by_md5": version_for_md5(recorded_md5, dataset, ctx) if recorded_md5 else None,
    }


class ReproduceIn(DatasetIn):
    rev: str = Field(description="Dataset revision to reproduce, e.g. 'data-v1.0'")
    checkout: bool = Field(
        default=False,
        description="If true, materialise it in an isolated scratch git worktree (never the main one)",
    )

    @field_validator("rev")
    @classmethod
    def _check_rev(cls, v: str) -> str:
        return _rev(v)


@tool(
    name="dvc_reproduce_instructions",
    system=SYSTEM,
    description=(
        "Exact commands to reproduce a dataset version; optionally check it out into an isolated scratch "
        "worktree (the main workspace is never modified)."
    ),
    input_model=ReproduceIn,
    timeout_seconds=90,
)
def dvc_reproduce_instructions(args: ReproduceIn, ctx: ToolContext) -> dict[str, Any]:
    info = pointer_at(args.rev, args.dataset, ctx)
    commands = [
        f"git checkout {args.rev} -- {args.dataset}.dvc",
        f"dvc checkout {args.dataset}.dvc   # or: dvc pull {args.dataset}.dvc",
        "dvc repro                           # re-runs validate -> preprocess -> train -> evaluate -> gate",
        f"git checkout HEAD -- {args.dataset}.dvc && dvc checkout {args.dataset}.dvc   # back to current",
    ]
    result: dict[str, Any] = {"rev": args.rev, "dataset": args.dataset, "expected_md5": info["md5"]}
    result["commands"] = commands
    if args.checkout:
        result["scratch_checkout"] = _scratch_checkout(args.rev, args.dataset, info["md5"], ctx)
    return result


def _scratch_checkout(rev: str, dataset: str, md5: str, ctx: ToolContext) -> dict[str, Any]:
    root = ctx.settings.repo_root
    cache_file = root / ".dvc" / "cache" / "files" / "md5" / md5[:2] / md5[2:]
    if not cache_file.exists():
        raise InvalidRequestError(f"md5 {md5} not in the local DVC cache; run 'dvc pull' first")
    name = f"{rev.replace('/', '_')}-{int(time.time())}"
    scratch = Path(tempfile.gettempdir()) / "mlops-agent-scratch" / name
    scratch.parent.mkdir(parents=True, exist_ok=True)
    run_cmd(["git", "worktree", "add", "--detach", str(scratch), rev], ctx, timeout=60)
    target = scratch / dataset
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(cache_file, target)
    actual = file_md5(target)
    return {"worktree": str(scratch), "data_file": str(target), "md5": actual, "md5_matches": actual == md5}
