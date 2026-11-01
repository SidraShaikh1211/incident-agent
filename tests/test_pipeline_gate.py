"""Gate, verification and PR-rendering behaviour — no model calls.

The golden set proves triage classifies correctly; these prove that what the
pipeline *does* with a verdict is right, including the cases that must never
open a PR.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from incident_agent.config import Settings
from incident_agent.models import (
    CodeLocation,
    EvidenceRef,
    FailureClass,
    FailureEvent,
    FixKind,
    FixResult,
    PRNarrative,
    RootCauseReport,
    StageRecord,
    TriageVerdict,
    VerifyResult,
)
from incident_agent.pipeline import Outcome, run_incident
from incident_agent.pr import attribution, branch_name, commit_message, render_pr_body
from incident_agent.verify import baseline_failures, verify

EVIDENCE = [EvidenceRef(line_number=1, excerpt="ERROR boom")]


def make_verdict(
    failure_class: FailureClass = FailureClass.SCHEMA_MISMATCH,
    fix_kind: FixKind = FixKind.CODE,
    confidence: float = 0.9,
    evidence: list[EvidenceRef] | None = None,
) -> TriageVerdict:
    return TriageVerdict(
        failure_class=failure_class,
        fix_kind=fix_kind,
        confidence=confidence,
        root_cause="the job references a column the source does not have",
        evidence=EVIDENCE if evidence is None else evidence,
        suggested_approach="rename the reference",
    )


def make_report(
    located: bool = True,
    confidence: float = 0.9,
    revised_class: FailureClass | None = None,
    confirms: bool = True,
    blockers: str = "",
) -> RootCauseReport:
    return RootCauseReport(
        located=located,
        confirms_classification=confirms,
        revised_class=revised_class,
        locations=(
            [CodeLocation(file_path="jobs/rollup.py", line_number=23, excerpt="F.col('event_ts')")]
            if located
            else []
        ),
        explanation="the job reads a column the source does not have",
        proposed_fix="reference event_timestamp",
        confidence=confidence,
        blockers=blockers,
    )


class StubSource:
    def __init__(self, event: FailureEvent) -> None:
        self._event = event

    def fetch(self) -> FailureEvent:
        return self._event


@pytest.fixture()
def event() -> FailureEvent:
    return FailureEvent(
        batch_id="b-1", job_name="daily_rollup", driver_log="ERROR boom\nsecond line\n"
    )


@pytest.fixture()
def settings() -> Settings:
    return Settings(anthropic_api_key="test", confidence_threshold=0.7)


# --- the gate ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("failure_class", "fix_kind"),
    [
        (FailureClass.MISSING_INPUT, FixKind.DATA),
        (FailureClass.PERMISSION, FixKind.INFRA),
        (FailureClass.TRANSIENT, FixKind.NONE),
        (FailureClass.UNKNOWN, FixKind.NONE),
    ],
)
def test_notify_only_classes_are_not_actionable(failure_class, fix_kind) -> None:
    verdict = make_verdict(failure_class=failure_class, fix_kind=fix_kind, confidence=0.99)
    actionable, why = verdict.is_actionable(0.7)
    assert not actionable
    assert "notify-only" in why


def test_low_confidence_is_not_actionable() -> None:
    actionable, why = make_verdict(confidence=0.55).is_actionable(0.7)
    assert not actionable
    assert "confidence" in why


def test_missing_evidence_is_not_actionable() -> None:
    actionable, why = make_verdict(evidence=[]).is_actionable(0.7)
    assert not actionable
    assert "no evidence" in why


def test_high_confidence_code_class_is_actionable() -> None:
    actionable, why = make_verdict().is_actionable(0.7)
    assert actionable and why == ""


async def test_pipeline_stops_before_touching_a_repo(monkeypatch, event, settings) -> None:
    """A notify-only verdict must not reach repo resolution at all."""
    monkeypatch.setattr(
        "incident_agent.pipeline.classify",
        _fake_triage(make_verdict(FailureClass.PERMISSION, FixKind.INFRA)),
    )

    def explode(*_a, **_kw):  # pragma: no cover - must never run
        raise AssertionError("repo resolution must not be reached")

    monkeypatch.setattr("incident_agent.pipeline.resolve_target", explode)

    record = await run_incident(StubSource(event), settings)
    assert record.outcome == Outcome.NO_PR_NOTIFY_ONLY
    assert record.pr_url is None


async def test_pipeline_reports_unresolved_repo(monkeypatch, event, settings) -> None:
    monkeypatch.setattr("incident_agent.pipeline.classify", _fake_triage(make_verdict()))
    monkeypatch.setattr("incident_agent.pipeline.resolve_target", lambda *a, **k: None)

    record = await run_incident(StubSource(event), settings)
    assert record.outcome == Outcome.NO_PR_UNRESOLVED_REPO
    assert "repo" in record.extra["gate_reason"]


def _fake_triage(verdict: TriageVerdict):
    """classify() returns (verdict, StageRecord)."""

    async def _classify(_event, _settings):
        return verdict, StageRecord(name="classify")

    return _classify


# --- the root-cause gate ----------------------------------------------------


def test_unlocated_root_cause_is_not_actionable() -> None:
    report = make_report(located=False, blockers="the cause is upstream of this repo")
    actionable, why = report.is_actionable(0.7)
    assert not actionable
    assert "not located" in why and "upstream" in why


def test_located_but_no_locations_is_not_actionable() -> None:
    report = make_report()
    report.locations = []
    actionable, why = report.is_actionable(0.7)
    assert not actionable
    assert "no code location" in why


def test_low_confidence_root_cause_is_not_actionable() -> None:
    actionable, why = make_report(confidence=0.5).is_actionable(0.7)
    assert not actionable
    assert "root-cause confidence" in why


def test_confident_located_root_cause_is_actionable() -> None:
    actionable, why = make_report().is_actionable(0.7)
    assert actionable and why == ""


async def test_pipeline_stops_when_root_cause_is_unlocated(
    monkeypatch, event, settings, tmp_path
) -> None:
    """An unlocated cause must never reach an agent that can write files."""
    monkeypatch.setattr("incident_agent.pipeline.classify", _fake_triage(make_verdict()))

    async def _rootcause(*_a, **_kw):
        return make_report(located=False, blockers="deployed revision differs"), StageRecord(
            name="rootcause"
        )

    monkeypatch.setattr("incident_agent.pipeline.find_root_cause", _rootcause)
    monkeypatch.setattr("incident_agent.pipeline.find_existing_pr", lambda *a, **k: None)

    def explode(*_a, **_kw):  # pragma: no cover - must never run
        raise AssertionError("the fix agent must not be started")

    monkeypatch.setattr("incident_agent.pipeline.apply_fix", explode)

    (tmp_path / "README.md").write_text("x")
    record = await run_incident(StubSource(event), settings, local_repo=tmp_path)

    assert record.outcome == Outcome.NO_PR_NOT_LOCATED
    assert record.pr_url is None
    assert "deployed revision differs" in record.extra["gate_reason"]


async def test_a_provider_failure_is_an_error_not_a_diagnosis(monkeypatch, event, settings) -> None:
    """A Vertex 503 used to become an `unknown` verdict, which the queue
    consumer then acked — the incident was silently lost. It must nack."""

    async def _classify(_event, _settings):
        verdict = make_verdict(FailureClass.UNKNOWN, FixKind.NONE, confidence=0.0, evidence=[])
        return verdict, StageRecord(name="classify", outcome="provider_error")

    monkeypatch.setattr("incident_agent.pipeline.classify", _classify)
    record = await run_incident(StubSource(event), settings)

    assert record.outcome == Outcome.ERROR
    assert "provider" in (record.error or "") and "classify" in (record.error or "")


class RecordingNotifier:
    def __init__(self) -> None:
        self.records: list = []

    def notify(self, record) -> None:
        self.records.append(record)


class RecordingStore:
    def __init__(self) -> None:
        self.saved: list = []

    def save(self, record) -> None:
        self.saved.append(record)


async def test_finished_runs_are_notified_and_stored(monkeypatch, event, settings) -> None:
    """In serve mode nobody reads stderr; a stopped run has to go somewhere."""
    monkeypatch.setattr(
        "incident_agent.pipeline.classify",
        _fake_triage(make_verdict(FailureClass.PERMISSION, FixKind.INFRA)),
    )
    notifier, store = RecordingNotifier(), RecordingStore()
    record = await run_incident(StubSource(event), settings, notifier=notifier, store=store)

    assert notifier.records == [record] and store.saved == [record]
    assert record.job_name == "daily_rollup"


async def test_inspection_runs_are_not_reported(monkeypatch, event, settings) -> None:
    monkeypatch.setattr("incident_agent.pipeline.classify", _fake_triage(make_verdict()))
    notifier = RecordingNotifier()
    await run_incident(StubSource(event), settings, stop_after="classify", notifier=notifier)
    assert notifier.records == []


async def test_a_failing_notifier_does_not_change_the_outcome(monkeypatch, event, settings) -> None:
    monkeypatch.setattr(
        "incident_agent.pipeline.classify",
        _fake_triage(make_verdict(FailureClass.TRANSIENT, FixKind.NONE)),
    )

    class Exploding:
        def notify(self, record):
            raise ConnectionError("webhook down")

    record = await run_incident(StubSource(event), settings, notifier=Exploding())
    assert record.outcome == Outcome.NO_PR_NOTIFY_ONLY


async def test_stage_costs_are_recorded(monkeypatch, event, settings) -> None:
    """Per-stage accounting is what makes cost attributable to an agent."""
    monkeypatch.setattr(
        "incident_agent.pipeline.classify",
        _fake_triage(make_verdict(FailureClass.TRANSIENT, FixKind.NONE)),
    )
    record = await run_incident(StubSource(event), settings)

    assert [s.name for s in record.stages] == ["classify"]
    assert record.total_cost_usd == 0.0


# --- verification -----------------------------------------------------------


def test_verify_reports_tier1_when_repo_has_no_tests(tmp_path: Path) -> None:
    (tmp_path / "job.py").write_text("x = 1\n")
    result = verify(tmp_path, ["job.py"])
    assert result.passed and result.tier == 1
    assert "weakly verified" in result.tier_description


def test_verify_fails_on_syntax_error(tmp_path: Path) -> None:
    (tmp_path / "job.py").write_text("def broken(:\n")
    result = verify(tmp_path, ["job.py"])
    assert not result.passed and result.tier == 0


def test_verify_reaches_tier2_when_tests_pass(tmp_path: Path) -> None:
    (tmp_path / "job.py").write_text("def add(a, b):\n    return a + b\n")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_job.py").write_text(
        "import sys; sys.path.insert(0, '.')\n"
        "from job import add\n"
        "def test_add():\n    assert add(1, 2) == 3\n"
    )
    result = verify(tmp_path, ["job.py"])
    assert result.passed and result.tier == 2


def test_verify_fails_when_tests_fail(tmp_path: Path) -> None:
    (tmp_path / "job.py").write_text("def add(a, b):\n    return a - b\n")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_job.py").write_text(
        "import sys; sys.path.insert(0, '.')\n"
        "from job import add\n"
        "def test_add():\n    assert add(1, 2) == 3\n"
    )
    result = verify(tmp_path, ["job.py"])
    assert not result.passed and result.tier == 1


def _repo_with_one_unrelated_broken_test(tmp_path: Path) -> None:
    (tmp_path / "job.py").write_text("def add(a, b):\n    return a + b\n")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_job.py").write_text(
        "import sys; sys.path.insert(0, '.')\n"
        "from job import add\n"
        "def test_add():\n    assert add(1, 2) == 3\n"
        "def test_unrelated_and_already_broken():\n    assert False\n"
    )


def test_baseline_names_the_tests_that_already_fail(tmp_path: Path) -> None:
    _repo_with_one_unrelated_broken_test(tmp_path)
    assert baseline_failures(tmp_path) == {"tests/test_job.py::test_unrelated_and_already_broken"}


def test_baseline_is_none_without_a_suite(tmp_path: Path) -> None:
    (tmp_path / "job.py").write_text("x = 1\n")
    assert baseline_failures(tmp_path) is None


def test_verify_does_not_blame_the_fix_for_a_preexisting_failure(tmp_path: Path) -> None:
    """One unrelated broken test used to reject every fix attempt."""
    _repo_with_one_unrelated_broken_test(tmp_path)
    baseline = baseline_failures(tmp_path)

    result = verify(tmp_path, ["job.py"], baseline=baseline)

    assert result.passed and result.tier == 1, result.output
    assert "failed before this change" in result.output
    assert "test_unrelated_and_already_broken" in result.output


def test_verify_still_fails_on_a_new_failure(tmp_path: Path) -> None:
    _repo_with_one_unrelated_broken_test(tmp_path)
    baseline = baseline_failures(tmp_path)
    (tmp_path / "job.py").write_text("def add(a, b):\n    return a - b\n")  # breaks test_add

    result = verify(tmp_path, ["job.py"], baseline=baseline)
    assert not result.passed and result.tier == 1


def test_verify_without_a_baseline_fails_on_any_failure(tmp_path: Path) -> None:
    _repo_with_one_unrelated_broken_test(tmp_path)
    assert not verify(tmp_path, ["job.py"]).passed


def test_verify_runs_repo_tests_without_the_orchestrators_secrets(tmp_path: Path, monkeypatch) -> None:
    """Test code comes from the cloned repo — and the fix agent may have written
    it. It used to run with the full environment, GITHUB_TOKEN included."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_0123456789abcdefghij")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-0123456789abcdefghij")
    (tmp_path / "job.py").write_text("x = 1\n")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_env.py").write_text(
        "import os\n"
        "def test_no_secrets():\n"
        "    assert 'GITHUB_TOKEN' not in os.environ, 'token leaked into repo tests'\n"
        "    assert 'ANTHROPIC_API_KEY' not in os.environ\n"
        f"    assert os.environ['HOME'] != {str(Path.home())!r}\n"
    )
    result = verify(tmp_path, ["job.py"])
    assert result.passed and result.tier == 2, result.output


# --- JVM projects -----------------------------------------------------------


def _scala_repo(tmp_path: Path, with_tests: bool = False) -> None:
    (tmp_path / "build.sbt").write_text('name := "job"\n')
    src = tmp_path / "src" / "main" / "scala"
    src.mkdir(parents=True)
    (src / "Job.scala").write_text("object Job\n")
    if with_tests:
        (tmp_path / "src" / "test" / "scala").mkdir(parents=True)


def _fake_sbt(monkeypatch) -> list[list[str]]:
    ran: list[list[str]] = []
    monkeypatch.setattr(
        "incident_agent.verify.shutil.which", lambda n: f"/usr/bin/{n}" if n == "sbt" else None
    )

    def fake_run(cmd, cwd, *, env, timeout=900):
        ran.append(cmd)
        return 0, f"$ {' '.join(cmd)}\nok"

    monkeypatch.setattr("incident_agent.verify._run", fake_run)
    return ran


def test_a_scala_change_is_compiled_when_sbt_is_present(tmp_path: Path, monkeypatch) -> None:
    _scala_repo(tmp_path)
    ran = _fake_sbt(monkeypatch)
    result = verify(tmp_path, ["src/main/scala/Job.scala"])
    assert result.passed and result.tier == 1
    assert ran == [["sbt", "-batch", "compile"]]


def test_scala_tests_reach_tier2(tmp_path: Path, monkeypatch) -> None:
    _scala_repo(tmp_path, with_tests=True)
    ran = _fake_sbt(monkeypatch)
    result = verify(tmp_path, ["src/main/scala/Job.scala"])
    assert result.passed and result.tier == 2
    assert ran == [["sbt", "-batch", "compile"], ["sbt", "-batch", "test"]]


def test_a_scala_change_without_a_build_tool_is_reported_unverified(tmp_path: Path, monkeypatch) -> None:
    _scala_repo(tmp_path)
    monkeypatch.setattr("incident_agent.verify.shutil.which", lambda _n: None)
    result = verify(tmp_path, ["src/main/scala/Job.scala"])
    assert result.tier == 0 and "Nothing about this change has been checked" in result.output


# --- over-escaped prose -----------------------------------------------------


def test_root_cause_prose_is_unescaped() -> None:
    """Structured output sometimes emits literal \\n and \\" inside strings.

    Left alone these render as visible backslashes in the pull request body.
    """
    report = RootCauseReport(
        located=True,
        confirms_classification=True,
        explanation='line one\\n\\nline two with a \\"quoted\\" word',
        proposed_fix="change \\\"event_ts\\\" to event_timestamp",
        confidence=0.9,
    )
    assert "\\n" not in report.explanation
    assert "line one\n\nline two" in report.explanation
    assert '"quoted"' in report.explanation
    assert '"event_ts"' in report.proposed_fix


def test_narrative_prose_is_unescaped() -> None:
    narrative = PRNarrative(
        title="fix: x",
        summary='the job read \\"event_ts\\"\\nwhich is gone',
        reviewer_focus="check upstream",
    )
    assert "\\n" not in narrative.summary
    assert '"event_ts"' in narrative.summary


def test_unescaping_leaves_ordinary_prose_alone() -> None:
    """A real backslash in a Windows path or regex must survive."""
    report = RootCauseReport(
        located=True,
        confirms_classification=True,
        explanation=r"the regex \d+ matched",
        confidence=0.9,
    )
    assert r"\d+" in report.explanation


# --- PR rendering -----------------------------------------------------------


def test_pr_body_surfaces_weak_verification(event) -> None:
    body = render_pr_body(
        event,
        make_verdict(),
        FixResult(rationale="renamed the column reference", files_changed=["jobs/x.py"]),
        VerifyResult(tier=1, passed=True, output="ruff ok"),
    )
    assert "Weakly verified" in body
    assert "Draft PR" in body
    assert "jobs/x.py" in body


def test_pr_body_warns_on_moderate_root_cause_confidence(event) -> None:
    body = render_pr_body(
        event,
        make_verdict(),
        FixResult(rationale="x"),
        VerifyResult(tier=2, passed=True, output="3 passed"),
        report=make_report(confidence=0.72),
    )
    assert "Moderate root-cause confidence" in body


def test_pr_body_is_clean_when_strongly_verified(event) -> None:
    body = render_pr_body(
        event,
        make_verdict(confidence=0.95),
        FixResult(rationale="x"),
        VerifyResult(tier=2, passed=True, output="3 passed"),
        report=make_report(confidence=0.95),
    )
    assert "Weakly verified" not in body
    assert "Moderate root-cause confidence" not in body


def test_pr_body_surfaces_a_revised_classification(event) -> None:
    """A reviewer must be told when the code contradicted the log-only verdict."""
    body = render_pr_body(
        event,
        make_verdict(FailureClass.EXECUTOR_OOM, FixKind.CONFIG),
        FixResult(rationale="x"),
        VerifyResult(tier=2, passed=True, output="ok"),
        report=make_report(
            confidence=0.9, revised_class=FailureClass.BAD_JOIN, confirms=False
        ),
    )
    assert "revised the classification" in body
    assert "executor_oom" in body and "bad_join" in body


def test_pr_body_uses_the_narrative_when_present(event) -> None:
    body = render_pr_body(
        event,
        make_verdict(),
        FixResult(rationale="x"),
        VerifyResult(tier=2, passed=True, output="ok"),
        report=make_report(),
        narrative=PRNarrative(
            title="fix(daily_rollup): read event_timestamp",
            summary="The job read a column that no longer exists.",
            reviewer_focus="Check whether other jobs read the same table.",
            residual_risk="Upstream rename is unconfirmed.",
        ),
    )
    assert "The job read a column that no longer exists." in body
    assert "Check whether other jobs read the same table." in body
    assert "Residual risk" in body


def test_pr_body_redacts_secrets_from_the_agent_rationale(event) -> None:
    body = render_pr_body(
        event,
        make_verdict(),
        FixResult(rationale="found password=hunter2SuperSecret in the config"),
        VerifyResult(tier=2, passed=True, output="ok"),
    )
    assert "hunter2SuperSecret" not in body


def test_branch_name_is_stable_and_safe(event) -> None:
    name = branch_name(event, make_verdict(), "incident-agent")
    assert name.startswith("incident-agent/schema_mismatch-daily-rollup-")
    assert " " not in name
    # Same failure -> same branch, so a recurring bug does not spawn branches.
    assert name == branch_name(event, make_verdict(), "incident-agent")


def test_commit_message_has_a_subject_within_limits(event) -> None:
    msg = commit_message(event, make_verdict(), model="gemini-3.5-flash")
    subject = msg.splitlines()[0]
    assert len(subject) <= 72
    assert "Co-Authored-By: gemini-3.5-flash" in msg
    assert f"Fingerprint: {event.fingerprint()}" in msg


def test_attribution_names_the_model_that_did_the_work() -> None:
    """A Gemini patch must not be signed by Claude, and an unknown model by nobody."""
    assert attribution("") == ""
    assert "Claude" not in commit_message(
        FailureEvent(batch_id="b", job_name="j", driver_log="x"), make_verdict()
    )
    assert attribution("claude-opus-4-8").endswith("<noreply@anthropic.com>")
    assert attribution("gemini-3.5-flash").startswith("Co-Authored-By: gemini-3.5-flash")
    assert "anthropic" not in attribution("gemini-3.5-flash")


def test_pr_body_attributes_the_actual_model(event) -> None:
    body = render_pr_body(
        event, make_verdict(), FixResult(rationale="x", model="gemini-3.5-flash"),
        VerifyResult(tier=2, passed=True, output="ok"),
    )
    assert "Co-Authored-By: gemini-3.5-flash" in body and "Claude" not in body
    assert f"`{event.fingerprint()}`" in body

    unattributed = render_pr_body(
        event, make_verdict(), FixResult(rationale="x"), VerifyResult(tier=2, passed=True)
    )
    assert "Co-Authored-By" not in unattributed


# --- verification must not overclaim ----------------------------------------


def test_verify_reports_tier0_when_nothing_could_be_checked(tmp_path: Path, monkeypatch) -> None:
    """Claiming 'static checks passed' when no check ran is a false statement
    on the artifact a human uses to decide whether to merge."""
    monkeypatch.setattr("incident_agent.verify.shutil.which", lambda _n: None)
    (tmp_path / "notes.txt").write_text("x")
    result = verify(tmp_path, ["notes.txt"])

    assert result.tier == 0
    assert result.commands_run == []
    assert "NOTHING WAS VERIFIED" in result.tier_description
    assert "Nothing about this change has been checked" in result.output


def test_verify_parses_config_files(tmp_path: Path) -> None:
    """Config-class fixes edit .conf/.yaml — linting Python says nothing about them."""
    (tmp_path / "spark.conf").write_text("spark.executor.memory 8g\nspark.x=1\n# note\n")
    result = verify(tmp_path, ["spark.conf"])
    assert result.passed and "parse spark.conf" in result.commands_run


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("app.yaml", "a: [1,2\n"),
        ("app.json", "{not json\n"),
        ("spark.conf", "garbage-with-no-separator\n"),
    ],
)
def test_verify_fails_on_malformed_config(tmp_path: Path, name: str, content: str) -> None:
    (tmp_path / name).write_text(content)
    result = verify(tmp_path, [name])
    assert not result.passed and result.tier == 0
