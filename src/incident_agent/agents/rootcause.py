"""Agent 2 — locate the cause in the repository, read-only.

This stage exists so the fix agent never searches blind. It gets Read/Grep/Glob
and nothing else: it cannot edit, and it cannot run commands. If it cannot
point at a file and explain the mechanism, the run stops here rather than
handing an unlocated hypothesis to an agent with write access.
"""

from __future__ import annotations

from pathlib import Path

import structlog

from ..config import Settings
from ..guardrails import make_pretooluse_hook
from ..models import FailureEvent, RootCauseReport, StageRecord, TriageVerdict
from ..providers import gemini
from ..redact import redact
from ..tools import RepoTools
from .base import agent_session
from .classify import number_lines

log = structlog.get_logger(__name__)

#: Read-only reconnaissance. No Edit/Write/Bash — this agent cannot change or
#: execute anything, which is what makes it safe to run before the gate.
READ_ONLY_TOOLS = ["Read", "Grep", "Glob"]


def build_prompt(
    event: FailureEvent,
    verdict: TriageVerdict,
    settings: Settings,
    *,
    subdir: str | None = None,
) -> str:
    evidence = (
        "\n".join(f"  line {e.line_number}: {e.excerpt}" for e in verdict.evidence)
        or "  (none cited)"
    )
    conf = (
        "\n".join(f"  {k} = {v}" for k, v in sorted(event.spark_conf.items()))
        or "  (not captured)"
    )

    log_text = redact(event.combined_log)
    if len(log_text) > settings.max_log_chars:
        log_text = log_text[: settings.max_log_chars] + "\n... [truncated] ..."

    sections = [
        f"# Locate the cause of a Spark failure: {event.job_name}",
        f"Batch `{event.batch_id}` failed at {event.occurred_at:%Y-%m-%d %H:%M:%S UTC}.",
        (
            "## Classifier verdict (a hypothesis from the log alone)\n"
            f"- Class: **{verdict.failure_class}** (fix kind: {verdict.fix_kind})\n"
            f"- Confidence: {verdict.confidence:.2f}\n"
            f"- Reasoning: {verdict.root_cause}\n"
            f"- Suggested approach: {verdict.suggested_approach or '(none)'}"
        ),
        f"## Evidence it cited\n{evidence}",
        f"## Spark configuration at failure\n{conf}",
        f"## Failure log (line-numbered)\n```\n{number_lines(log_text)}\n```",
    ]

    if subdir:
        sections.append(
            f"## Scope\nThe Spark job lives under `{subdir}/` in this repository. "
            f"Confine your search there — other directories hold unrelated services."
        )

    sections.append(
        "Find the code that produced this failure. Read it. Then report whether "
        "you located it, where, and why that code produces this log."
    )
    return "\n\n".join(sections)


async def find_root_cause(
    worktree: Path,
    event: FailureEvent,
    verdict: TriageVerdict,
    settings: Settings,
    *,
    subdir: str | None = None,
) -> tuple[RootCauseReport, StageRecord]:
    """Locate the failure in the repository. Never raises."""
    violations: list[str] = []
    # Read-only by tool list, but the hook still runs: it is the thing that
    # guarantees the constraint rather than the tool list being trusted.
    hook = make_pretooluse_hook(
        worktree=worktree,
        editable_globs=[],  # nothing is editable at this stage
        protected_globs=settings.protected_globs,
        violations=violations,
    )

    system_prompt = settings.prompt("rootcause_system.md")
    user_prompt = build_prompt(event, verdict, settings, subdir=subdir)

    if settings.provider == "gemini":
        # Gemini has no built-in file tools, so RepoTools supplies them and
        # enforces the guardrails in its dispatcher. read_only=True is what
        # makes this stage incapable of editing or executing anything.
        tools = RepoTools(
            worktree=worktree,
            editable_globs=[],
            protected_globs=settings.protected_globs,
            read_only=True,
            violations=violations,
        )
        report, outcome = await gemini.agent_loop(
            stage="rootcause",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            tools=tools,
            settings=settings,
            model=settings.gemini_rootcause_model,
            output_model=RootCauseReport,
            max_steps=settings.gemini_max_steps,
        )
        record = outcome.record
        record.steps = outcome.steps
    else:
        report, session = await agent_session(
            stage="rootcause",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            worktree=worktree,
            model=settings.rootcause_model,
            effort=settings.rootcause_effort,
            allowed_tools=READ_ONLY_TOOLS,
            max_turns=settings.max_turns,
            max_budget_usd=settings.max_budget_usd,
            hook=hook,
            violations=violations,
            output_model=RootCauseReport,
            env=settings.provider_env(),
        )
        record = session.record

    if report is None:
        report = RootCauseReport(
            located=False,
            confirms_classification=False,
            explanation="The root-cause agent returned no usable report.",
            confidence=0.0,
            blockers=record.outcome or "no structured output",
        )

    log.info(
        "rootcause_report",
        located=report.located,
        confirms=report.confirms_classification,
        revised_class=report.revised_class,
        confidence=report.confidence,
        files=[loc.file_path for loc in report.locations],
    )
    if not report.confirms_classification:
        log.warning(
            "rootcause_contradicts_classifier",
            classifier=verdict.failure_class,
            revised=report.revised_class,
        )

    return report, record
