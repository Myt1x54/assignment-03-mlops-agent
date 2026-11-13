"""Load ``params.yaml`` (shared by DVC stages, the Airflow DAG and manual runs)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PARAMS = ROOT / "params.yaml"


def load_params(path: Path = DEFAULT_PARAMS) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)
