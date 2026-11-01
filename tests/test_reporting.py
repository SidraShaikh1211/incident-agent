"""Notifications and the run store — the two places a finished run goes.

Neither existed before: a run that stopped at a gate was a line on stderr,
and `RunRecord`'s docstring promised persistence nothing provided.
"""

from __future__ import annotations

import json
from pathlib import Path

from incident_agent.config import Settings
from incident_agent.models import (
    EvidenceRef,
    FailureClass,
    FixKind,
    RunRecord,
    TriageVerdict,
    VerifyResult,
)
from incident_agent.notify import (
    LogNotifier,
    WebhookNotifier,
    build_notifier,
    format_notification,
)
from incident_agent.store import JsonFileRunStore, NullRunStore, build_store


def _record(outcome: str = "notify_only") -> RunRecord:
    return RunRecord(
        run_id="run0001",
        batch_id="batch-42",
        fingerprint="abcd1234abcd1234",
        job_name="daily_rollup",
        outcome=outcome,
        verdict=TriageVerdict(
            failure_class=FailureClass.PERMISSION,
            fix_kind=FixKind.INFRA,
            confidence=0.93,
            root_cause="the runner service account lacks storage.objects.create",
            evidence=[EvidenceRef(line_number=9, excerpt="403 Forbidden")],
        ),
        extra={"gate_reason": "class 'permission' is notify-only"},
    )


# --- formatting -------------------------------------------------------------


def test_notification_carries_what_an_engineer_needs() -> None:
    text = format_notification(_record())
    assert "notify_only" in text
    assert "daily_rollup" in text and "batch-42" in text
    assert "permission @ 0.93" in text
    assert "storage.objects.create" in text
    assert "notify-only" in text


def test_notification_includes_pr_verification_and_error() -> None:
    record = _record("pr_opened")
    record.pr_url = "https://github.com/org/repo/pull/5"
    record.verify = VerifyResult(tier=2, passed=True)
    text = format_notification(record)
    assert "pull/5" in text and "tier 2" in text

    record = _record("error")
    record.error = "RuntimeError: provider failed"
    assert "provider failed" in format_notification(record)


def test_notification_is_redacted() -> None:
    record = _record()
    record.extra["gate_reason"] = "saw password=hunter2hunter2 in the log"
    assert "hunter2hunter2" not in format_notification(record)


# --- transports -------------------------------------------------------------


def test_webhook_posts_slack_compatible_json(monkeypatch) -> None:
    sent: dict = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"ok"

    def fake_urlopen(request, timeout):
        sent["url"] = request.full_url
        sent["method"] = request.get_method()
        sent["content_type"] = request.get_header("Content-type")
        sent["body"] = json.loads(request.data)
        return FakeResponse()

    monkeypatch.setattr("incident_agent.notify.urllib.request.urlopen", fake_urlopen)
    WebhookNotifier("https://hooks.example.test/abc").notify(_record("verify_failed"))

    assert sent["url"] == "https://hooks.example.test/abc"
    assert sent["method"] == "POST"
    assert sent["content_type"] == "application/json"
    assert "verify_failed" in sent["body"]["text"]


def test_default_notifier_is_the_log_and_webhook_is_opt_in() -> None:
    assert isinstance(build_notifier(Settings(provider="gemini")), LogNotifier)
    notifier = build_notifier(Settings(provider="gemini", notify_webhook_url="https://h.test/x"))
    assert isinstance(notifier, WebhookNotifier) and notifier.url == "https://h.test/x"


# --- the store --------------------------------------------------------------


def test_json_store_round_trips_a_record(tmp_path: Path) -> None:
    store = JsonFileRunStore(tmp_path / "runs")
    record = _record("pr_opened")
    store.save(record)

    loaded = store.load("run0001")
    assert loaded.outcome == "pr_opened"
    assert loaded.verdict and loaded.verdict.failure_class == FailureClass.PERMISSION
    assert [r.run_id for r in store.recent()] == ["run0001"]


def test_json_store_recent_is_empty_before_any_run(tmp_path: Path) -> None:
    assert JsonFileRunStore(tmp_path / "nope").recent() == []


def test_store_is_off_by_default_and_json_when_asked() -> None:
    assert isinstance(build_store(Settings(provider="gemini")), NullRunStore)
    store = build_store(Settings(provider="gemini", run_store="json", run_store_dir="/tmp/x"))
    assert isinstance(store, JsonFileRunStore) and store.directory == Path("/tmp/x")
    assert isinstance(build_store(Settings(provider="gemini", run_store="bogus")), NullRunStore)
