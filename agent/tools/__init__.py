"""Agent tools. ``load_all()`` imports every tool module so its ``@tool`` registrations reach ``REGISTRY``."""

from __future__ import annotations

import importlib

TOOL_MODULES = (
    "agent.tools.dvc_tools",
    "agent.tools.mlflow_tools",
    "agent.tools.airflow_tools",
    "agent.tools.kubernetes_tools",
)


def load_all() -> None:
    for module in TOOL_MODULES:
        importlib.import_module(module)
