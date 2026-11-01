"""Agent 1 — classify the failure against the taxonomy.

No tools, no repository access. Its job is to be cheap and to stop the run
early for failures that no code change can fix: a missing input partition, an
IAM denial, a preempted node. Everything downstream costs repo access and real
model time, so this gate is what keeps the pipeline honest.
"""

from __future__ import annotations

import structlog

from ..config import Settings
from ..models import FailureClass, FailureEvent, FixKind, StageRecord, TriageVerdict
from ..providers import gemini
from ..redact import redact, redaction_findings
from .base import structured_call

log = structlog.get_logger(__name__)


def number_lines(text: str) -> str:
    """Prefix each line with a 1-based number so the model can cite evidence."""
    return "\n".join(f"{i:>5} | {line}" for i, line in enumerate(text.splitlines(), start=1))


def build_system_prompt(settings: Settings) -> str:
    return settings.prompt("triage_system.md").replace("{taxonomy}", settings.prompt("taxonomy.md"))


def build_user_prompt(event: FailureEvent) -> str:
    parts = [f"Job: {event.job_name}", f"Batch: {event.batch_id}"]
    if event.spark_conf:
        conf = "\n".join(f"  {k} = {v}" for k, v in sorted(event.spark_conf.items()))
        parts.append(f"Spark configuration:\n{conf}")
    parts.append("Log (line-numbered — cite these numbers as evidence):\n")
    parts.append(number_lines(redact(event.combined_log)))
    return "\n\n".join(parts)


def _unknown(reason: str) -> TriageVerdict:
    return TriageVerdict(
        failure_class=FailureClass.UNKNOWN,
        fix_kind=FixKind.NONE,
        confidence=0.0,
        root_cause=reason,
    )


async def classify(
    event: FailureEvent, settings: Settings
) -> tuple[TriageVerdict, StageRecord]:
    """Classify ``event``. Never raises — an unreadable log yields ``unknown``."""
    if not event.combined_log.strip():
        return _unknown("No log content was available for this failure."), StageRecord(
            name="classify", outcome="empty_log"
        )

    if found := redaction_findings(event.combined_log):
        log.info("redacted_secrets_from_log", patterns=found)

    if settings.provider == "gemini":
        verdict, record = await gemini.structured_call(
            stage="classify",
            system_prompt=build_system_prompt(settings),
            user_prompt=build_user_prompt(event),
            output_model=TriageVerdict,
            settings=settings,
            model=settings.gemini_classify_model,
        )
    else:
        verdict, record = await structured_call(
            stage="classify",
            system_prompt=build_system_prompt(settings),
            user_prompt=build_user_prompt(event),
            output_model=TriageVerdict,
            model=settings.triage_model,
            effort=settings.triage_effort,
            env=settings.provider_env(),
        )

    if verdict is None:
        return _unknown("Classification produced no usable verdict."), record

    # Taxonomy rule 1, enforced in code rather than trusted to the prompt.
    if not verdict.evidence and verdict.failure_class is not FailureClass.UNKNOWN:
        log.warning("classify_no_evidence", original_class=verdict.failure_class)
        verdict = verdict.model_copy(
            update={
                "failure_class": FailureClass.UNKNOWN,
                "fix_kind": FixKind.NONE,
                "confidence": 0.0,
                "root_cause": (
                    f"Downgraded to unknown: classified as '{verdict.failure_class}' "
                    f"without citing log evidence. Original reasoning: {verdict.root_cause}"
                ),
            }
        )

    log.info(
        "classify_verdict",
        failure_class=verdict.failure_class,
        fix_kind=verdict.fix_kind,
        confidence=verdict.confidence,
        evidence_lines=[e.line_number for e in verdict.evidence],
    )
    return verdict, record
