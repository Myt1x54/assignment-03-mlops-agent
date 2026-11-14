"""Rebuild the DVC cache (and local remote) on a fresh clone (README "Data").

The DVC remote of this project is a local folder (``../mlops-dvc-remote``, DECISION D6/D30) that does
not travel with the GitHub repo. The datasets are generated deterministically (seeded), so this script
regenerates every tagged version, **verifies the md5 against the ``.dvc`` pointer stored at each git
tag**, places the file in the DVC cache, checks out the current version and pushes everything to the
local remote.

    uv run python scripts/bootstrap_data.py
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from training.make_dataset import generate, write_csv  # noqa: E402

POINTER = "data/raw/churn.csv.dvc"
VERSIONS = {"data-v1.0": "v1", "data-v2.0": "v2"}


def sh(*cmd: str) -> str:
    return subprocess.run(list(cmd), cwd=ROOT, check=True, capture_output=True, text=True).stdout  # noqa: S603


def md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()  # noqa: S324 - DVC content hash


def main() -> int:
    (ROOT.parent / "mlops-dvc-remote").mkdir(exist_ok=True)
    cache = ROOT / ".dvc" / "cache" / "files" / "md5"
    with tempfile.TemporaryDirectory() as tmp:
        for tag, version in VERSIONS.items():
            expected = yaml.safe_load(sh("git", "show", f"{tag}:{POINTER}"))["outs"][0]["md5"]
            out = Path(tmp) / f"{version}.csv"
            write_csv(generate(version), out)
            actual = md5(out)
            if actual != expected:
                print(f"{tag}: regenerated md5 {actual} != pointer {expected} (library versions differ?)")
                return 1
            target = cache / expected[:2] / expected[2:]
            if target.exists() and md5(target) == expected:
                print(f"{tag}: md5 {expected} verified (already in cache)")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(out, target)
            target.chmod(0o444)  # DVC keeps cache files read-only
            print(f"{tag}: md5 {expected} verified and cached")
    py = [sys.executable, "-m", "dvc"]
    subprocess.run([*py, "checkout", POINTER], cwd=ROOT, check=True)  # noqa: S603
    subprocess.run([*py, "push", "--all-tags"], cwd=ROOT, check=True)  # noqa: S603
    print("DVC cache and local remote rebuilt; workspace data matches the current pointer.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
