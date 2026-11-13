"""S1 smoke tests: the environment and repository layout are what the plan expects."""

import sys
from pathlib import Path

import pydantic
import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_python_version_is_pinned_312() -> None:
    assert sys.version_info[:2] == (3, 12)


def test_core_dependencies_importable() -> None:
    assert pydantic.VERSION.startswith("2.")
    assert yaml.safe_load("a: 1") == {"a": 1}


def test_required_layout_exists() -> None:
    required = [
        "agent/tools",
        "api",
        "dags",
        "data",
        "training",
        "kubernetes",
        "tests",
        "screenshots",
        ".github/workflows",
    ]
    missing = [p for p in required if not (ROOT / p).is_dir()]
    assert not missing, f"missing folders: {missing}"


def test_env_example_has_no_real_secrets() -> None:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for line in text.splitlines():
        if any(k in line for k in ("API_KEY=", "PASSWORD=")):
            assert line.split("=", 1)[1] == "replace-me", line


def test_env_file_is_gitignored() -> None:
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in ignored
