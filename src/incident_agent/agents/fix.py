"""Agent 3 — patch the located cause and verify it.

The only stage with write access. It receives a specific file and line from the
root-cause agent, so it is not searching — it is changing one known thing.
Every tool call still passes the PreToolUse hook.
"""

from __future__ import annotations

from pathlib import Path

import structlog

from ..config import Settings
from ..guardrails import make_pretooluse_hook
from ..models import FailureEvent, FixResult, RootCauseReport, StageRecord, TriageVerdict
from ..providers import gemini
from ..redact import redact
from ..tools import RepoTools
from .base import agent_session

log = structlog.get_logger(__name__)

#: Write access plus the ability to run the repo's tests. No network tools.
FIX_TOOLS = ["Read", "Grep", "Glob", "Edit", "Write", "Bash", "TodoWrite"]


def build_prompt(
    event: FailureEvent,
    verdict: TriageVerdict,
    report: RootCauseReport,
    settings: Settings,
    *,
    subdir: str | None = None,
    previous_attempt: str | None = None,
) -> str:
    locations = "\n".join(
        f"  - `{loc.file_path}`"
        + (f" line {loc.line_number}" if loc.line_number else "")
        + (f"\n      {loc.excerpt}" if loc.excerpt else "")
        for loc in report.locations
    ) or "  (none)"

    effective_class = report.revised_class or verdict.failure_class

    log_text = redact(event.combined_log)
    if len(log_text) > settings.max_log_chars // 2:
        log_text = log_text[: settings.max_log_chars // 2] + "\n... [truncated] ..."

    sections = [
        f"# Fix a Spark failure: {event.job_name}",
        f"Batch `{event.batch_id}` failed at {event.occurred_at:%Y-%m-%d %H:%M:%S UTC}.",
        (
            f"## Failure class\n**{effective_class}** (fix kind: {verdict.fix_kind})"
            + (
                f"\n\nNote: the root-cause analysis revised this from "
                f"`{verdict.failure_class}` after reading the code."
                if report.revised_class
                else ""
            )
        ),
        (
            "## Root cause — already located for you\n"
            f"{report.explanation}\n\n"
            f"**Confidence:** {report.confidence:.2f}\n\n"
            f"**Location(s):**\n{locations}\n\n"
            f"**Proposed approach:** {report.proposed_fix or '(none given)'}"
        ),
        f"## Failure log (for reference)\n```\n{log_text}\n```",
    ]

    if subdir:
        sections.append(
            f"## Scope\nOnly files under `{subdir}/` are in scope. Edits elsewhere "
            f"will be refused."
        )

    if previous_attempt:
        sections.append(
            "## Your previous attempt failed verification\n"
            f"{previous_attempt}\n\n"
            "The worktree has been reset. Work out why that approach did not "
            "work before trying again — do not simply repeat the same edit."
        )

    sections.append(
        "Apply the fix, then run the repository's tests if it has any. "
        "Finish with the summary described in your instructions."
    )
    return "\n\n".join(sections)


async def apply_fix(
    worktree: Path,
    event: FailureEvent,
    verdict: TriageVerdict,
    report: RootCauseReport,
    settings: Settings,
    *,
    subdir: str | None = None,
    previous_attempt: str | None = None,
) -> tuple[FixResult, list[str], StageRecord]:
    """Run one fix attempt. Returns the result, guardrail violations, accounting."""
    violations: list[str] = []
    editable = settings.editable_globs
    if subdir:
        # Confine edits to the Spark job's subtree in the monorepo.
        editable = [f"{subdir.rstrip('/')}/{glob}" for glob in settings.editable_globs]

    system_prompt = settings.prompt("fix_system.md")
    user_prompt = build_prompt(
        event, verdict, report, settings, subdir=subdir, previous_attempt=previous_attempt
    )

    if settings.provider == "gemini":
        tools = RepoTools(
            worktree=worktree,
            editable_globs=editable,
            protected_globs=settings.protected_globs,
            read_only=False,
            violations=violations,
        )
        _, outcome = await gemini.agent_loop(
            stage="fix",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            tools=tools,
            settings=settings,
            model=settings.gemini_fix_model,
            max_steps=settings.gemini_max_steps,
        )
        record = outcome.record
        record.steps = outcome.steps
        text = outcome.text
        if outcome.hit_step_limit:
            log.warning("fix_hit_step_limit", steps=outcome.steps)
    else:
        hook = make_pretooluse_hook(
            worktree=worktree,
            editable_globs=editable,
            protected_globs=settings.protected_globs,
            violations=violations,
        )
        _, session = await agent_session(
            stage="fix",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            worktree=worktree,
            model=settings.fix_model,
            effort=settings.fix_effort,
            allowed_tools=FIX_TOOLS,
            max_turns=settings.max_turns,
            max_budget_usd=settings.max_budget_usd,
            hook=hook,
            violations=violations,
            env=settings.provider_env(),
        )
        record = session.record
        text = session.text

    result = FixResult(
        rationale=text,
        cost_usd=record.cost_usd,
        stopped_reason=record.outcome,
        model=record.model,
    )

    if violations:
        log.warning("guardrail_violations", count=len(violations), details=violations[:10])

    return result, violations, record


def summarise_violations(violations: list[str]) -> str:
    """Collapse repeated denials for the PR body."""
    if not violations:
        return ""
    counts: dict[str, int] = {}
    for v in violations:
        counts[v] = counts.get(v, 0) + 1
    return "\n".join(
        f"- {v}" + (f" (×{n})" if n > 1 else "") for v, n in counts.items()
    )
