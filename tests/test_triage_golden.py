"""Golden-set regression for triage.

Triage precision decides whether an automated agent is allowed to modify
pipeline code, so it needs a real regression suite rather than spot checks.
Each fixture asserts the class, the auto-PR decision, and that evidence was
actually cited.

These make live model calls. They are marked `golden` and excluded from the
default run:

    uv run pytest -m golden          # run them
    uv run pytest                    # everything else

Grow the corpus from real captured failures — that is what makes this test
worth having.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from incident_agent.agents.classify import classify
from incident_agent.config import get_settings
from incident_agent.models import FailureClass, FixKind
from incident_agent.sources.local import FileLogSource

FIXTURES = Path(__file__).parent / "fixtures" / "logs"

def _provider_available() -> str | None:
    """Reason to skip, or None. Gemini needs ADC; Claude needs the CLI."""
    settings = get_settings()
    if settings.provider == "gemini":
        adc = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
        if adc.exists() or os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
            return None
        return "no Application Default Credentials for Vertex"
    if shutil.which("claude") is None:
        return "claude CLI not available"
    return None


_SKIP = _provider_available()
pytestmark = [
    pytest.mark.golden,
    pytest.mark.skipif(_SKIP is not None, reason=_SKIP or ""),
]

# fixture -> (expected class, expected fix kind, should reach the fix stage)
GOLDEN: dict[str, tuple[FailureClass, FixKind, bool]] = {
    "schema_mismatch.log": (FailureClass.SCHEMA_MISMATCH, FixKind.CODE, True),
    "executor_oom.log": (FailureClass.EXECUTOR_OOM, FixKind.CONFIG, True),
    "missing_input.log": (FailureClass.MISSING_INPUT, FixKind.DATA, False),
    "permission.log": (FailureClass.PERMISSION, FixKind.INFRA, False),
    "transient.log": (FailureClass.TRANSIENT, FixKind.NONE, False),
    # Scala/JVM: no Python traceback, so the user-code signal is a frame naming
    # the job's own package. The target repo is a Scala Spark app, so this case
    # matters as much as the PySpark ones.
    "scala_null_handling.log": (FailureClass.NULL_HANDLING, FixKind.CODE, True),
}


@pytest.mark.parametrize(("fixture", "expected"), sorted(GOLDEN.items()))
async def test_triage_classifies_fixture(
    fixture: str, expected: tuple[FailureClass, FixKind, bool]
) -> None:
    expected_class, expected_kind, should_act = expected
    settings = get_settings()

    event = FileLogSource(FIXTURES / fixture).fetch()
    verdict, _ = await classify(event, settings)

    assert verdict.failure_class == expected_class, (
        f"{fixture}: expected {expected_class}, got {verdict.failure_class} "
        f"— {verdict.root_cause}"
    )
    assert verdict.fix_kind == expected_kind, f"{fixture}: fix_kind was {verdict.fix_kind}"
    assert verdict.evidence, f"{fixture}: no evidence cited"
    assert verdict.root_cause.strip(), f"{fixture}: empty root cause"

    actionable, why_not = verdict.is_actionable(settings.confidence_threshold)
    assert actionable is should_act, (
        f"{fixture}: gate said actionable={actionable} (expected {should_act}); {why_not}"
    )


@pytest.mark.parametrize("fixture", sorted(GOLDEN))
async def test_cited_evidence_matches_the_log(fixture: str) -> None:
    """A citation pointing at the wrong line is worse than no citation."""
    settings = get_settings()
    event = FileLogSource(FIXTURES / fixture).fetch()
    verdict, _ = await classify(event, settings)

    lines = event.combined_log.splitlines()
    for ref in verdict.evidence:
        assert 1 <= ref.line_number <= len(lines), (
            f"{fixture}: cited line {ref.line_number}, log has {len(lines)} lines"
        )
        actual = lines[ref.line_number - 1]
        # The model may quote a fragment rather than the whole line.
        excerpt = ref.excerpt.strip().rstrip(".")
        assert excerpt[:40] in actual or actual.strip()[:40] in ref.excerpt, (
            f"{fixture}: line {ref.line_number} cited as {ref.excerpt!r} "
            f"but the log line is {actual!r}"
        )


async def test_notify_only_classes_never_reach_the_fix_stage() -> None:
    """The gate is what stops the agent patching code for an IAM problem."""
    settings = get_settings()
    for fixture, (_, _, should_act) in GOLDEN.items():
        if should_act:
            continue
        event = FileLogSource(FIXTURES / fixture).fetch()
        verdict, _ = await classify(event, settings)
        actionable, _ = verdict.is_actionable(settings.confidence_threshold)
        assert not actionable, f"{fixture} would have opened a PR"
