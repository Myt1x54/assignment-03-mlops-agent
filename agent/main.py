"""Conversational CLI for the MLOps agent.

    uv run python -m agent.main                          # interactive session
    uv run python -m agent.main --once "Which model is deployed?"
    uv run python -m agent.main --once "..." --transcript docs/report/transcripts/scenario.md

State-changing tools pause and ask *you* (on the terminal) to type ``yes``; the LLM can never approve.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt

from agent.config import REPO_ROOT, get_settings
from agent.llm import GeminiClient, LLMError
from agent.logging_utils import redact, setup_logging
from agent.orchestrator import Orchestrator, Turn

TRANSCRIPT_DIR = REPO_ROOT / "docs" / "report" / "transcripts"
console = Console()


def terminal_confirm(summary: str, action: dict[str, Any]) -> bool:
    console.print(
        Panel(
            f"[bold]{summary}[/bold]\n\n{json.dumps(action, indent=2)}",
            title="Confirmation required (state-changing action)",
            border_style="yellow",
        )
    )
    answer = Prompt.ask("Type 'yes' to execute, anything else to cancel", default="no")
    # Strip a UTF-8 BOM: Windows PowerShell 5.1 prefixes text piped into native programs with one.
    return answer.lstrip("\ufeff").strip().lower() == "yes"


def render(turn: Turn) -> None:
    assert turn.response is not None
    for call, result in zip(turn.calls, turn.results, strict=True):
        style = "green" if result.status.value == "ok" else "red"
        status = f"{result.status.value} ({result.duration_ms} ms)"
        console.print(f"  [{style}]{result.evidence_id} {call.name}[/{style}] -> {status}")
    console.print(Markdown(turn.response.answer))


def transcript_markdown(turns: list[Turn], model: str) -> str:
    out = [f"# Agent transcript ({datetime.now().isoformat(timespec='seconds')})", "", f"LLM: `{model}`", ""]
    for i, turn in enumerate(turns, 1):
        assert turn.response is not None
        out += [f"## Q{i}: {turn.question}", "", f"`request_id={turn.response.request_id}` "
                f"iterations={turn.response.iterations} tool_calls={turn.response.tool_calls} "
                f"stopped={turn.response.stopped_reason}", "", "### Tool calls", ""]  # fmt: skip
        for call, result in zip(turn.calls, turn.results, strict=True):
            args = json.dumps(call.args, default=str)
            status = f"`{result.status.value}` ({result.duration_ms} ms)"
            out.append(f"- **{result.evidence_id}** `{call.name}({args})` -> {status}")
        for c in turn.confirmations:
            verdict = "approved" if c["approved"] else "declined"
            out.append(f"- confirmation for `{c['tool']}`: {c['summary']} -> **{verdict}**")
        out += ["", "### Answer", "", turn.response.answer, ""]
    return redact("\n".join(out))


def save_transcript(turns: list[Turn], model: str, path: Path | None) -> Path:
    if path is None:
        slug = re.sub(r"[^a-z0-9]+", "-", turns[0].question.lower())[:50].strip("-") or "session"
        path = TRANSCRIPT_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(transcript_markdown(turns, model), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MLOps AI agent (DVC + MLflow + Airflow + Kubernetes)")
    parser.add_argument(
        "--once",
        metavar="QUESTION",
        action="append",
        help="answer this question and exit (repeat for a scripted multi-turn session)",
    )
    parser.add_argument("--wait", type=float, default=0.0, help="seconds to pause between --once questions")
    parser.add_argument(
        "--transcript", type=Path, help="transcript path (default: docs/report/transcripts/...)"
    )
    parser.add_argument("--no-transcript", action="store_true")
    parser.add_argument("--log-level", default=None)
    args = parser.parse_args(argv)

    settings = get_settings()
    setup_logging(args.log_level or settings.log_level)
    try:
        llm = GeminiClient(
            settings.gemini_api_key.get_secret_value(),
            settings.agent_model,
            fallback_models=settings.fallback_models,
        )
    except LLMError as exc:
        console.print(f"[red]{exc}[/red]")
        return 2
    agent = Orchestrator(llm, settings=settings, confirm=terminal_confirm)
    turns: list[Turn] = []

    questions = list(args.once) if args.once else None
    banner = f"MLOps agent ready - model `{settings.agent_model}`, namespace `{settings.k8s_namespace}`."
    console.print(Panel(f"{banner} Type 'exit' to quit.", border_style="cyan"))
    while True:
        if questions is not None:
            if not questions:
                break
            if turns and args.wait:
                time.sleep(args.wait)
            question = questions.pop(0)
            console.print(f"[bold cyan]You:[/bold cyan] {question}")
        else:
            try:
                question = Prompt.ask("[bold cyan]You[/bold cyan]").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if question.lower() in {"exit", "quit", ":q"}:
                break
            if not question:
                continue
        with console.status("thinking / calling tools..."):
            turn = agent.ask(question)
        turns.append(turn)
        render(turn)

    if turns and not args.no_transcript:
        path = save_transcript(turns, settings.agent_model, args.transcript)
        console.print(f"[dim]transcript saved to {path}[/dim]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
