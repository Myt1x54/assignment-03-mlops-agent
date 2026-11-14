"""S9: DVC tools with mocked subprocess / MLflow (no real git, dvc or network)."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

import agent.tools.dvc_tools as dvc
from agent.config import Settings, load_policy
from agent.errors import PermissionDeniedError
from agent.schemas import ToolStatus
from agent.tools.base import ToolContext, execute_tool

V1_MD5 = "1b459e76699765537ccd9c84dde369b1"
V2_MD5 = "8ed597e7a87a65293692274aa5a22d34"
SEP = "\x1f"


def pointer(md5: str, size: int) -> str:
    return f"outs:\n- md5: {md5}\n  size: {size}\n  hash: md5\n  path: churn.csv\n"


class FakeGitDvc:
    """Answers the handful of allow-listed commands the tools issue."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        args = argv[3:] if argv[1:3] == ["-m", "dvc"] else argv[1:]
        prog = "dvc" if argv[1:3] == ["-m", "dvc"] else argv[0]
        out = ""
        if prog == "git" and args[0] == "rev-parse":
            out = "abc123\n"
        elif prog == "git" and args[0] == "tag":
            out = (
                f"data-v2.0{SEP}2026-11-13T23:07:59+05:00{SEP}Dataset v2\n"
                f"data-v1.0{SEP}2026-11-13T23:07:46+05:00{SEP}Dataset v1\n"
            )
        elif prog == "git" and args[0] == "show":
            out = pointer(V1_MD5, 333981) if args[1].startswith("data-v1.0") else pointer(V2_MD5, 540196)
        elif prog == "git" and args[0] == "log":
            out = f"ae6d3df{SEP}2026-11-13T23:07:59+05:00{SEP}S2: dataset v2\n"
        elif prog == "git" and args[0] == "status":
            out = ""
        elif prog == "dvc" and args[0] == "diff":
            out = json.dumps({"added": [], "modified": [{"path": "data\\raw\\churn.csv"}], "deleted": []})
        elif prog == "dvc" and args[0] == "status":
            out = "{}"
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")


@pytest.fixture
def fake(monkeypatch) -> FakeGitDvc:
    f = FakeGitDvc()
    monkeypatch.setattr(dvc.subprocess, "run", f)
    return f


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    data = tmp_path / "data" / "raw"
    data.mkdir(parents=True)
    content = b"customer_id,churn\n1,0\n"
    (data / "churn.csv").write_bytes(content)
    md5 = hashlib.md5(content).hexdigest()
    (data / "churn.csv.dvc").write_text(pointer(md5, len(content)), encoding="utf-8")
    return tmp_path


@pytest.fixture
def ctx(repo: Path) -> ToolContext:
    return ToolContext(
        request_id="req-dvc", settings=Settings(_env_file=None, repo_root=repo), policy=load_policy()
    )


def test_compare_versions_reports_modification(fake, ctx):
    r = execute_tool("dvc_compare_versions", {"rev_a": "data-v1.0", "rev_b": "data-v2.0"}, ctx)
    assert r.status is ToolStatus.OK
    assert r.data["dvc_diff"] == {"modified": ["data/raw/churn.csv"]}
    assert r.data["a"]["md5"] == V1_MD5
    assert r.data["b"]["md5"] == V2_MD5
    assert r.data["dataset_changed"] is True
    assert r.data["size_delta_bytes"] == 540196 - 333981


def test_current_version_untagged_but_in_sync(fake, ctx):
    r = execute_tool("dvc_get_current_version", {}, ctx)
    assert r.status is ToolStatus.OK
    assert r.data["workspace_in_sync"] is True
    assert r.data["data_version_tag"] == "untagged"  # tmp data md5 matches no tag
    assert r.data["git_commit"] == "abc123"


def test_list_dataset_versions_newest_first(fake, ctx):
    r = execute_tool("dvc_list_dataset_versions", {}, ctx)
    assert [v["tag"] for v in r.data["tagged_versions"]] == ["data-v2.0", "data-v1.0"]
    assert r.data["latest_version"] == "data-v2.0"
    assert r.data["pointer_commits"][0]["commit"] == "ae6d3df"


def test_detect_changes_in_sync(fake, ctx):
    r = execute_tool("dvc_detect_changes", {}, ctx)
    assert r.data == {
        "in_sync": True,
        "stale_stages": [],
        "dvc_status": {},
        "uncommitted_pointer_changes": [],
    }


def test_list_tracked_datasets_reads_pointer_files(ctx):
    r = execute_tool("dvc_list_tracked_datasets", {}, ctx)
    assert r.data["tracked_files"][0]["data_path"] == "data/raw/churn.csv"


def test_dvc_runs_via_python_module_with_shell_false(fake, ctx):
    execute_tool("dvc_detect_changes", {}, ctx)
    dvc_call = next(c for c in fake.calls if "dvc" in c)
    assert dvc_call[1:4] == ["-m", "dvc", "status"]


def test_disallowed_command_is_rejected(ctx):
    with pytest.raises(PermissionDeniedError):
        dvc.run_cmd(["dvc", "push"], ctx)
    with pytest.raises(PermissionDeniedError):
        dvc.run_cmd(["git", "checkout", "data-v1.0"], ctx)


@pytest.mark.parametrize("rev", ["--output=/etc/passwd", "a..b", "v1; rm -rf /", ""])
def test_option_or_range_injection_in_revisions_is_invalid(ctx, rev):
    r = execute_tool("dvc_compare_versions", {"rev_a": rev}, ctx)
    assert r.status is ToolStatus.ERROR
    assert r.error.type == "ValidationError"


def test_path_traversal_dataset_is_invalid(ctx):
    r = execute_tool("dvc_get_current_version", {"dataset": "../../secrets.csv"}, ctx)
    assert r.status is ToolStatus.ERROR


def test_missing_binary_is_unavailable(monkeypatch, ctx):
    def missing(*a, **k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(dvc.subprocess, "run", missing)
    assert execute_tool("dvc_get_current_version", {}, ctx).status is ToolStatus.UNAVAILABLE


def test_run_dataset_verified_against_git(fake, monkeypatch, ctx):
    import agent.tools.mlflow_tools as mt

    tags = {"data.version": "data-v2.0", "data.dvc_md5": V2_MD5, "data.path": "data/raw/churn.csv"}
    monkeypatch.setattr(mt, "get_run_tags", lambda run_id, c: tags)
    r = execute_tool("dvc_get_run_dataset", {"run_id": "a" * 32}, ctx)
    assert r.status is ToolStatus.OK
    assert r.data["data_version"] == "data-v2.0"
    assert r.data["verified_against_git"] is True


def test_run_dataset_md5_mismatch_is_flagged(fake, monkeypatch, ctx):
    import agent.tools.mlflow_tools as mt

    tags = {"data.version": "data-v2.0", "data.dvc_md5": "f" * 32}
    monkeypatch.setattr(mt, "get_run_tags", lambda run_id, c: tags)
    r = execute_tool("dvc_get_run_dataset", {"run_id": "b" * 32}, ctx)
    assert r.data["verified_against_git"] is False


def test_run_dataset_when_mlflow_down_is_unavailable(monkeypatch, ctx):
    import agent.tools.mlflow_tools as mt
    from agent.errors import ServiceUnavailableError

    def down(run_id, c):
        raise ServiceUnavailableError("MLflow server unreachable")

    monkeypatch.setattr(mt, "get_run_tags", down)
    assert execute_tool("dvc_get_run_dataset", {"run_id": "c" * 32}, ctx).status is ToolStatus.UNAVAILABLE
