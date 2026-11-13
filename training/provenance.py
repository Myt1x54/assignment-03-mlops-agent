"""Dataset provenance: link a training run to the exact DVC-tracked dataset version (DECISION D7).

Recorded on every MLflow run as tags:

``data.version``   release tag whose ``.dvc`` pointer has the same md5 (e.g. ``data-v2.0``) or ``untagged``
``data.dvc_md5``   md5 from ``data/raw/churn.csv.dvc``
``data.file_md5``  md5 of the actual file used (must equal ``data.dvc_md5`` when in sync)
``data.in_sync``   ``true`` if the workspace file matches its DVC pointer
``data.path``      tracked path
``git.commit``     commit of the code + ``.dvc`` pointer
``dvc.remote``     configured default DVC remote URL
"""

from __future__ import annotations

import configparser
import hashlib
import os
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = Path("data/raw/churn.csv")
DVC_POINTER = Path("data/raw/churn.csv.dvc")
TAG_PATTERN = "data-*"


def _git(*args: str, cwd: Path = ROOT, timeout: int = 15) -> str | None:
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", *args],  # noqa: S607
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def file_md5(path: Path) -> str:
    digest = hashlib.md5()  # noqa: S324 - md5 used as a content fingerprint, as DVC does
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pointer_md5(text: str) -> str | None:
    data = yaml.safe_load(text) or {}
    outs = data.get("outs") or []
    return outs[0].get("md5") if outs else None


def version_for_md5(md5: str | None, root: Path = ROOT) -> str:
    """Return the newest ``data-*`` tag whose pointer file has this md5, else ``untagged``."""
    if not md5:
        return "untagged"
    tags = _git("tag", "--list", TAG_PATTERN, "--sort=-creatordate", cwd=root)
    for tag in (tags or "").splitlines():
        content = _git("show", f"{tag}:{DVC_POINTER.as_posix()}", cwd=root)
        if content and pointer_md5(content) == md5:
            return tag
    return "untagged"


def dvc_remote_url(root: Path = ROOT) -> str:
    cfg_path = root / ".dvc" / "config"
    if not cfg_path.exists():
        return "none"
    cfg = configparser.ConfigParser()
    cfg.read(cfg_path, encoding="utf-8")
    default = cfg.get("core", "remote", fallback=None)
    for section in cfg.sections():
        if section.startswith("'remote") and (default is None or default in section):
            return cfg.get(section, "url", fallback="unknown")
    return "none"


def collect(root: Path = ROOT, data_path: Path = DATA_PATH) -> dict[str, str]:
    pointer = root / DVC_POINTER
    dvc_md5 = pointer_md5(pointer.read_text(encoding="utf-8")) if pointer.exists() else None
    actual = root / data_path
    actual_md5 = file_md5(actual) if actual.exists() else None
    commit = _git("rev-parse", "HEAD", cwd=root) or os.environ.get("GIT_COMMIT", "unknown")
    return {
        "data.version": version_for_md5(dvc_md5, root),
        "data.dvc_md5": dvc_md5 or "unknown",
        "data.file_md5": actual_md5 or "missing",
        "data.in_sync": str(bool(dvc_md5) and dvc_md5 == actual_md5).lower(),
        "data.path": data_path.as_posix(),
        "git.commit": commit,
        "dvc.remote": dvc_remote_url(root),
    }


if __name__ == "__main__":
    for key, value in collect().items():
        print(f"{key:15} {value}")
