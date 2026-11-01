"""Secrets must never reach a prompt or a PR body.

The corresponding fixture (`secrets_in_log.log`) is deliberately seeded with
credential-shaped strings; `test_fixture_log_is_fully_redacted` is the one that
would catch a regression in a real captured log.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from incident_agent.agents.classify import build_user_prompt
from incident_agent.models import FailureEvent
from incident_agent.redact import normalise_for_fingerprint, redact, redaction_findings
from incident_agent.sources.local import FileLogSource

FIXTURES = Path(__file__).parent / "fixtures" / "logs"

SECRETS = [
    "hunter2SuperSecret",
    "sk-ant-api03-AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHH",
    "ghp_AbCdEf0123456789AbCdEf0123456789AbCd",
    "dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk",
    "deadbeefcafe1234567890abcdef",
]


@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        ("password=hunter2", "hunter2"),
        ("api_key: sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWX", "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWX"),
        ("token=ghp_AbCdEf0123456789AbCdEf0123456789AbCd", "ghp_AbCdEf0123456789AbCdEf0123456789AbCd"),
        ("AWS key AKIAIOSFODNN7EXAMPLE here", "AKIAIOSFODNN7EXAMPLE"),
        ("key AIzaSyA1234567890abcdefghijklmnopqrstuv0", "AIzaSyA1234567890abcdefghijklmnopqrstuv0"),
        ("jdbc:postgresql://h:5432/db?user=u&password=s3cr3tvalue", "s3cr3tvalue"),
        ("postgres://admin:letmein@warehouse:5432/db", "letmein"),
        ("Authorization: Bearer abc123def456ghi", "abc123def456ghi"),
        ("?X-Goog-Signature=abcdef0123456789", "abcdef0123456789"),
    ],
)
def test_redacts_credential_shapes(raw: str, secret: str) -> None:
    cleaned = redact(raw)
    assert secret not in cleaned, f"{secret!r} survived redaction of {raw!r}"


def test_preserves_diagnostic_content() -> None:
    line = "AnalysisException: cannot resolve `event_ts` given input columns [user_id]"
    assert redact(line) == line


def test_fixture_log_is_fully_redacted() -> None:
    raw = (FIXTURES / "secrets_in_log.log").read_text()
    assert redaction_findings(raw), "fixture should contain detectable secrets"

    cleaned = redact(raw)
    for secret in SECRETS:
        assert secret not in cleaned, f"{secret!r} leaked through redact()"

    # The NPE that makes the log diagnostically useful must survive.
    assert "NullPointerException" in cleaned


def test_secrets_never_reach_the_triage_prompt() -> None:
    """End-to-end: the assembled prompt is what actually leaves the process."""
    event = FileLogSource(FIXTURES / "secrets_in_log.log").fetch()
    prompt = build_user_prompt(event)

    for secret in SECRETS:
        assert secret not in prompt, f"{secret!r} reached the triage prompt"


# --- fingerprinting ---------------------------------------------------------


def _event(log: str) -> FailureEvent:
    return FailureEvent(batch_id="b", job_name="j", driver_log=log)


def test_fingerprint_is_stable_across_runs() -> None:
    """The same bug failing nightly must produce one fingerprint, not many."""
    monday = _event(
        "26/07/20 03:00:29 ERROR Executor: Exception in task 4.0 in stage 3.0 (TID 812)\n"
        "container_1753063200000_0233_01_000008 on 10.128.0.19\n"
        "AnalysisException: cannot resolve `event_ts`"
    )
    tuesday = _event(
        "26/07/21 03:02:41 ERROR Executor: Exception in task 9.0 in stage 7.0 (TID 991)\n"
        "container_1753149600000_0311_01_000014 on 10.128.0.77\n"
        "AnalysisException: cannot resolve `event_ts`"
    )
    assert monday.fingerprint() == tuesday.fingerprint()


def test_fingerprint_distinguishes_different_bugs() -> None:
    a = _event("ERROR AnalysisException: cannot resolve `event_ts`")
    b = _event("ERROR Container killed by YARN for exceeding physical memory limits")
    assert a.fingerprint() != b.fingerprint()


def test_normalise_drops_info_noise_but_keeps_errors() -> None:
    text = (
        "26/07/22 03:00:11 INFO SparkContext: Running Spark version 3.5.1\n"
        "26/07/22 03:00:12 DEBUG BlockManager: something chatty\n"
        "26/07/22 03:00:29 ERROR Executor: boom\n"
    )
    out = normalise_for_fingerprint(text)
    assert "boom" in out
    assert "Running Spark version" not in out
    assert "something chatty" not in out
