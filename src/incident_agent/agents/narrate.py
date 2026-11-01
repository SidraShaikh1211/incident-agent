"""Agent 4 — compose the pull request narrative.

Prose only. Branching, committing and pushing stay in :mod:`incident_agent.pr`,
which is deterministic code — the agent never holds the push credential.
"""

from __future__ import annotations

import structlog

from ..config import Settings
from ..models import (
    FailureEvent,
    FixResult,
    PRNarrative,
    RootCauseReport,
    StageRecord,
    TriageVerdict,
    VerifyResult,
)
from ..providers import gemini
from ..redact import redact
from .base import structured_call

log = structlog.get_logger(__name__)

MAX_DIFF_CHARS = 12_000


def build_prompt(
    event: FailureEvent,
    verdict: TriageVerdict,
    report: RootCauseReport,
    fix: FixResult,
    verify: VerifyResult,
) -> str:
    diff = fix.diff
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n... [diff truncated] ..."

    locations = "\n".join(
        f"  - {loc.file_path}" + (f":{loc.line_number}" if loc.line_number else "")
        for loc in report.locations
    ) or "  (none)"

    effective_class = report.revised_class or verdict.failure_class
    revision_note = (
        f"\nThe root-cause stage revised the class from `{verdict.failure_class}` "
        f"to `{report.revised_class}` after reading the code."
        if report.revised_class
        else ""
    )

    return "\n\n".join(
        [
            f"# Job: {event.job_name}   Batch: {event.batch_id}",
            (
                f"## Classification\n**{effective_class}**, classifier confidence "
                f"{verdict.confidence:.2f}.{revision_note}\n\n{verdict.root_cause}"
            ),
            (
                f"## Root cause (confidence {report.confidence:.2f})\n"
                f"{report.explanation}\n\nLocated at:\n{locations}"
            ),
            f"## What the fix agent reported\n{fix.rationale or '(no summary)'}",
            f"## Diff\n```diff\n{diff}\n```",
            (
                f"## Verification\nTier {verify.tier} — {verify.tier_description}\n"
                f"Commands: {', '.join(verify.commands_run) or 'none'}\n\n"
                f"```\n{verify.output[:3000]}\n```"
            ),
            "Write the PR description fields.",
        ]
    )


async def narrate(
    event: FailureEvent,
    verdict: TriageVerdict,
    report: RootCauseReport,
    fix: FixResult,
    verify: VerifyResult,
    settings: Settings,
) -> tuple[PRNarrative, StageRecord]:
    """Compose the PR copy. Falls back to a factual stub if the model fails."""
    system_prompt = settings.prompt("pr_system.md")
    user_prompt = redact(build_prompt(event, verdict, report, fix, verify))

    if settings.provider == "gemini":
        narrative, record = await gemini.structured_call(
            stage="narrate",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            output_model=PRNarrative,
            settings=settings,
            model=settings.gemini_narrate_model,
        )
    else:
        narrative, record = await structured_call(
            stage="narrate",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            output_model=PRNarrative,
            model=settings.narrate_model,
            effort=settings.narrate_effort,
            env=settings.provider_env(),
        )

    if narrative is None:
        effective_class = report.revised_class or verdict.failure_class
        log.warning("narrate_fallback", reason=record.outcome)
        narrative = PRNarrative(
            title=f"fix({event.job_name}): {str(effective_class).replace('_', ' ')}",
            summary=report.explanation or verdict.root_cause,
            reviewer_focus=(
                "The PR narrative could not be generated — review the diff and the "
                "root-cause section below directly."
            ),
            residual_risk=f"Verification reached tier {verify.tier} only.",
        )

    log.info("pr_narrative", title=narrative.title)
    return narrative, record
