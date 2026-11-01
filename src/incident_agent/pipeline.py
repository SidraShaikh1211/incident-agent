"""Orchestrates the four agents.

    classify → [taxonomy gate] → root cause → [located gate] → fix → verify → narrate → PR

Each gate can end the run. Stopping early with a diagnosis is a correct
outcome — the failure modes this system must avoid are a confident wrong patch
and a PR nobody can evaluate, not "did not produce a PR".
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import structlog

from .agents import apply_fix, classify, find_root_cause, narrate, summarise_violations
from .config import PROJECT_ROOT, Settings
from .models import (
    FailureEvent,
    RootCauseReport,
    RunRecord,
    StageRecord,
    TriageVerdict,
    VerifyResult,
)
from .notify import Notifier, build_notifier
from .pr import find_existing_pr, open_pull_request
from .repo import RepoTarget, Worktree, resolve_target
from .sources.base import LogSource
from .store import RunStore, build_store
from .verify import baseline_failures, verify

log = structlog.get_logger(__name__)

REGISTRY_PATH = PROJECT_ROOT / "config" / "job_registry.yaml"


class Outcome:
    PR_OPENED = "pr_opened"
    DRY_RUN = "dry_run"
    NO_PR_NOTIFY_ONLY = "notify_only"
    NO_PR_LOW_CONFIDENCE = "low_confidence"
    NO_PR_UNRESOLVED_REPO = "unresolved_repo"
    NO_PR_NOT_LOCATED = "root_cause_not_located"
    NO_PR_NO_CHANGE = "no_change"
    NO_PR_VERIFY_FAILED = "verify_failed"
    NO_PR_DUPLICATE = "duplicate"
    ERROR = "error"


class ProviderError(RuntimeError):
    """The inference provider failed after retries.

    Raised so the run ends as :attr:`Outcome.ERROR` and the queue consumer
    nacks the message. Swallowing it would turn a Vertex 503 into an
    ``unknown`` verdict that gets acked — a lost incident.
    """


def _check_provider(stage: StageRecord) -> None:
    if stage.outcome == "provider_error":
        raise ProviderError(f"inference provider failed during {stage.name}")


async def run_incident(
    source: LogSource,
    settings: Settings,
    *,
    local_repo: str | Path | None = None,
    stop_after: str | None = None,
    notifier: Notifier | None = None,
    store: RunStore | None = None,
) -> RunRecord:
    """Run one incident through all four agents. Never raises for expected stops.

    ``stop_after`` short-circuits after ``classify`` or ``rootcause``, which is
    what the CLI's inspection commands use; those inspection runs are not
    reported or stored. Everything else is handed to the notifier and the run
    store once finished, whatever the outcome.
    """
    record = await _run_incident(source, settings, local_repo=local_repo, stop_after=stop_after)
    if stop_after is None:
        _report_outcome(record, settings, notifier=notifier, store=store)
    return record


def _report_outcome(
    record: RunRecord,
    settings: Settings,
    *,
    notifier: Notifier | None,
    store: RunStore | None,
) -> None:
    """Persist and announce a finished run. Neither may change the outcome."""
    try:
        (store or build_store(settings)).save(record)
    except Exception as exc:  # noqa: BLE001 - reporting must not fail the run
        log.warning("run_store_failed", run_id=record.run_id, error=str(exc)[:300])
    try:
        (notifier or build_notifier(settings)).notify(record)
    except Exception as exc:  # noqa: BLE001
        log.warning("notify_failed", run_id=record.run_id, error=str(exc)[:300])


async def _run_incident(
    source: LogSource,
    settings: Settings,
    *,
    local_repo: str | Path | None,
    stop_after: str | None,
) -> RunRecord:
    record = RunRecord(run_id=uuid.uuid4().hex[:12], batch_id="?", fingerprint="?")

    try:
        event: FailureEvent = source.fetch()
        record.batch_id = event.batch_id
        record.job_name = event.job_name
        record.fingerprint = event.fingerprint()
        log.info(
            "incident_start",
            run_id=record.run_id,
            batch=event.batch_id,
            job=event.job_name,
            fingerprint=record.fingerprint,
        )

        # --- agent 1: classify -------------------------------------------
        verdict, stage = await classify(event, settings)
        record.verdict = verdict
        record.stages.append(stage)
        _check_provider(stage)

        if stop_after == "classify":
            record.outcome = "classify_only"
            return _finish(record)

        actionable, why_not = verdict.is_actionable(settings.confidence_threshold)
        if not actionable:
            log.info("gate_stopped", stage="classify", reason=why_not)
            record.outcome = (
                Outcome.NO_PR_LOW_CONFIDENCE
                if "confidence" in why_not
                else Outcome.NO_PR_NOTIFY_ONLY
            )
            record.extra["gate_reason"] = why_not
            return _finish(record)

        # --- locate the repository ---------------------------------------
        target: RepoTarget | None = resolve_target(
            event, registry_path=REGISTRY_PATH, local_repo=local_repo
        )
        if target is None:
            record.outcome = Outcome.NO_PR_UNRESOLVED_REPO
            record.extra["gate_reason"] = (
                "could not map this job to a repository; add a `repo` label to the "
                "Dataproc batch or an entry in config/job_registry.yaml"
            )
            return _finish(record)

        with Worktree(target, github_token=settings.github_token) as worktree:
            return await _analyse_and_fix(
                worktree, event, verdict, settings, record, stop_after=stop_after
            )

    except Exception as exc:  # noqa: BLE001 - top-level boundary
        log.exception("incident_failed", run_id=record.run_id)
        record.outcome = Outcome.ERROR
        record.error = f"{type(exc).__name__}: {exc}"
        return _finish(record)


async def _analyse_and_fix(
    worktree: Worktree,
    event: FailureEvent,
    verdict: TriageVerdict,
    settings: Settings,
    record: RunRecord,
    *,
    stop_after: str | None = None,
) -> RunRecord:
    assert worktree.path is not None

    subdir = _resolve_subdir(worktree.path, settings)

    # --- agent 2: root cause (read-only) ---------------------------------
    report, stage = await find_root_cause(
        worktree.path, event, verdict, settings, subdir=subdir
    )
    record.root_cause = report
    record.stages.append(stage)
    _check_provider(stage)

    if stop_after == "rootcause":
        record.outcome = "rootcause_only"
        return _finish(record)

    located, why_not = report.is_actionable(settings.rootcause_threshold)
    if not located:
        log.info("gate_stopped", stage="rootcause", reason=why_not)
        record.outcome = Outcome.NO_PR_NOT_LOCATED
        record.extra["gate_reason"] = why_not
        return _finish(record)

    existing = find_existing_pr(worktree, record.fingerprint, settings.github_token)
    if existing:
        log.info("duplicate_pr_exists", url=existing)
        record.outcome = Outcome.NO_PR_DUPLICATE
        record.pr_url = existing
        return _finish(record)

    return await _fix_verify_pr(
        worktree, event, verdict, report, settings, record, subdir=subdir
    )


async def _fix_verify_pr(
    worktree: Worktree,
    event: FailureEvent,
    verdict: TriageVerdict,
    report: RootCauseReport,
    settings: Settings,
    record: RunRecord,
    *,
    subdir: str | None,
) -> RunRecord:
    assert worktree.path is not None

    previous_attempt: str | None = None
    all_violations: list[str] = []
    last_verify = VerifyResult()

    # Tests that already fail before the agent touches anything. Without this a
    # repo with one unrelated broken test rejects every fix three times over.
    baseline: set[str] | None = None
    if settings.verify_baseline:
        baseline = baseline_failures(worktree.path)
        if baseline:
            record.extra["preexisting_test_failures"] = sorted(baseline)
            log.warning("preexisting_test_failures", count=len(baseline))

    for attempt in range(1, settings.max_fix_iterations + 1):
        log.info("fix_attempt", attempt=attempt, of=settings.max_fix_iterations)

        # --- agent 3: fix (write access) ---------------------------------
        fix, violations, stage = await apply_fix(
            worktree.path,
            event,
            verdict,
            report,
            settings,
            subdir=subdir,
            previous_attempt=previous_attempt,
        )
        record.stages.append(stage)
        _check_provider(stage)
        all_violations.extend(violations)
        fix.iterations = attempt

        changed = worktree.changed_files()
        fix.files_changed = changed
        fix.changed = bool(changed)
        fix.diff = worktree.diff() if changed else ""

        if not changed:
            log.info("agent_made_no_changes", attempt=attempt)
            record.outcome = Outcome.NO_PR_NO_CHANGE
            record.extra["agent_summary"] = fix.rationale
            return _finish(record)

        last_verify = verify(worktree.path, changed, baseline=baseline)
        record.verify = last_verify

        if last_verify.passed:
            # --- agent 4: narrate ----------------------------------------
            narrative, narrate_stage = await narrate(
                event, verdict, report, fix, last_verify, settings
            )
            record.stages.append(narrate_stage)
            record.extra["attempts"] = attempt

            record.pr_url = open_pull_request(
                worktree,
                event,
                verdict,
                fix,
                last_verify,
                settings,
                report=report,
                narrative=narrative,
                violations=summarise_violations(all_violations),
            )
            record.outcome = Outcome.DRY_RUN if settings.dry_run else Outcome.PR_OPENED
            return _finish(record)

        log.warning("verify_failed", attempt=attempt, tier=last_verify.tier)
        previous_attempt = (
            f"Attempt {attempt} changed: {', '.join(changed)}\n\n"
            f"Your summary was:\n{fix.rationale[:2000]}\n\n"
            f"Verification failed:\n```\n{last_verify.output[:4000]}\n```"
        )
        worktree.reset()

    record.outcome = Outcome.NO_PR_VERIFY_FAILED
    record.verify = last_verify
    record.extra["attempts"] = settings.max_fix_iterations
    return _finish(record)


def _resolve_subdir(worktree: Path, settings: Settings) -> str | None:
    """Use the configured Spark subtree only if it exists in this checkout."""
    subdir = (settings.spark_subdir or "").strip("/")
    if not subdir:
        return None
    if (worktree / subdir).is_dir():
        return subdir
    log.info("spark_subdir_absent", configured=subdir, scope="whole repository")
    return None


def _finish(record: RunRecord) -> RunRecord:
    record.finished_at = datetime.now(UTC)
    log.info(
        "incident_done",
        run_id=record.run_id,
        outcome=record.outcome,
        pr=record.pr_url,
        cost_usd=round(record.total_cost_usd, 4),
        stages=[s.name for s in record.stages],
    )
    return record
