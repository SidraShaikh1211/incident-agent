"""The push path, against a real local git remote.

`open_pull_request` had no coverage: every run so far was `--dry-run`, so the
branch/commit/push sequence had never actually executed. These tests run it
against a bare repository standing in for GitHub, which exercises everything
except the final `gh pr create` call (that one needs GitHub and is faked here).

What this proves: the agent's edits reach a named branch on a remote, the diff
is exactly what the agent changed, and dry-run really does push nothing.
"""

from __future__ import annotations

import json
import subprocess
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
    RootCauseReport,
    TriageVerdict,
    VerifyResult,
)
from incident_agent.pr import find_existing_pr, open_pull_request
from incident_agent.repo import GitError, RepoTarget, Worktree, git


def _run(*args: str, cwd: Path) -> str:
    return subprocess.run(
        args, cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture()
def origin(tmp_path: Path) -> Path:
    """A bare repo standing in for GitHub, seeded with one commit."""
    bare = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "jobs").mkdir()
    (seed / "jobs" / "rollup.py").write_text("col = 'event_ts'\n")

    git("init", "-q", "-b", "main", cwd=seed)
    git("add", "-A", cwd=seed)
    git("-c", "user.email=a@b", "-c", "user.name=t", "commit", "-q", "-m", "seed", cwd=seed)
    subprocess.run(
        ["git", "clone", "--bare", "-q", str(seed), str(bare)], check=True, capture_output=True
    )
    return bare


@pytest.fixture()
def event() -> FailureEvent:
    return FailureEvent(batch_id="b-1", job_name="daily_rollup", driver_log="ERROR boom\n")


def _verdict() -> TriageVerdict:
    return TriageVerdict(
        failure_class=FailureClass.SCHEMA_MISMATCH,
        fix_kind=FixKind.CODE,
        confidence=0.9,
        root_cause="stale column reference",
        evidence=[EvidenceRef(line_number=1, excerpt="ERROR boom")],
    )


def _report() -> RootCauseReport:
    return RootCauseReport(
        located=True,
        confirms_classification=True,
        locations=[CodeLocation(file_path="jobs/rollup.py", line_number=1)],
        explanation="the job reads a column that no longer exists",
        confidence=0.9,
    )


def _open(worktree: Worktree, settings: Settings, monkeypatch, gh_ok: bool = True):
    """Run open_pull_request with the GitHub call faked, git left real."""
    calls: list[list[str]] = []

    def fake_gh(args, cwd, token):
        calls.append(args)
        if args[:2] == ["pr", "list"]:
            return 0, "[]"
        return (0, "https://github.com/org/repo/pull/42\n") if gh_ok else (1, "gh exploded")

    monkeypatch.setattr("incident_agent.pr._gh", fake_gh)
    url = open_pull_request(
        worktree,
        event=FailureEvent(batch_id="b-1", job_name="daily_rollup", driver_log="ERROR boom\n"),
        verdict=_verdict(),
        fix=FixResult(rationale="renamed the column", files_changed=["jobs/rollup.py"]),
        verify_result=VerifyResult(tier=2, passed=True, output="3 passed"),
        settings=settings,
        report=_report(),
    )
    return url, calls


def test_push_creates_a_branch_on_the_remote_with_the_agents_diff(
    origin: Path, monkeypatch
) -> None:
    settings = Settings(dry_run=False, draft_pr=True, provider="gemini")

    with Worktree(RepoTarget(slug="org/repo", ref=None, remote_url=str(origin))) as worktree:
        # Worktree clones from the bare repo; simulate the agent's edit.
        (worktree.path / "jobs" / "rollup.py").write_text("col = 'event_timestamp'\n")

        url, calls = _open(worktree, settings, monkeypatch)

    assert url == "https://github.com/org/repo/pull/42"

    # The branch exists on the remote and carries exactly the agent's change.
    branches = _run("git", "branch", "--list", cwd=origin)
    assert "incident-agent/schema_mismatch-daily-rollup-" in branches

    branch = next(
        line.strip().lstrip("* ")
        for line in branches.splitlines()
        if "incident-agent/" in line
    )
    content = _run("git", "show", f"{branch}:jobs/rollup.py", cwd=origin)
    assert content.strip() == "col = 'event_timestamp'"

    subject = _run("git", "log", "-1", "--format=%s", branch, cwd=origin).strip()
    assert subject.startswith("fix(daily_rollup):")


def test_pr_is_opened_as_a_draft(origin: Path, monkeypatch) -> None:
    """The agent must never open a PR that looks ready to merge."""
    settings = Settings(dry_run=False, draft_pr=True, provider="gemini")

    with Worktree(RepoTarget(slug="org/repo", ref=None, remote_url=str(origin))) as worktree:
        (worktree.path / "jobs" / "rollup.py").write_text("col = 'x'\n")
        _, calls = _open(worktree, settings, monkeypatch)

    create = next(c for c in calls if c[:2] == ["pr", "create"])
    assert "--draft" in create


def test_dry_run_pushes_nothing(origin: Path, monkeypatch) -> None:
    settings = Settings(dry_run=True, provider="gemini")

    with Worktree(RepoTarget(slug="org/repo", ref=None, remote_url=str(origin))) as worktree:
        (worktree.path / "jobs" / "rollup.py").write_text("col = 'x'\n")
        url, calls = _open(worktree, settings, monkeypatch)

    assert url is None
    assert calls == [], "dry run must not call gh at all"
    assert "incident-agent/" not in _run("git", "branch", "--list", cwd=origin)


def test_a_failed_gh_call_raises_rather_than_reporting_success(
    origin: Path, monkeypatch
) -> None:
    """A silent failure here would report a PR that does not exist."""
    settings = Settings(dry_run=False, provider="gemini")

    with Worktree(RepoTarget(slug="org/repo", ref=None, remote_url=str(origin))) as worktree:
        (worktree.path / "jobs" / "rollup.py").write_text("col = 'x'\n")
        with pytest.raises(RuntimeError, match="gh pr create failed"):
            _open(worktree, settings, monkeypatch, gh_ok=False)


def test_the_pushed_diff_excludes_build_artifacts(origin: Path, monkeypatch) -> None:
    """__pycache__ from a test run must not end up in the pull request."""
    settings = Settings(dry_run=False, provider="gemini")

    with Worktree(RepoTarget(slug="org/repo", ref=None, remote_url=str(origin))) as worktree:
        (worktree.path / "jobs" / "rollup.py").write_text("col = 'y'\n")
        cache = worktree.path / "jobs" / "__pycache__"
        cache.mkdir()
        (cache / "rollup.cpython-313.pyc").write_bytes(b"\x00binary")

        _open(worktree, settings, monkeypatch)

    branches = _run("git", "branch", "--list", cwd=origin)
    branch = next(
        line.strip().lstrip("* ") for line in branches.splitlines() if "incident-agent/" in line
    )
    files = _run("git", "show", "--name-only", "--format=", branch, cwd=origin)
    assert "jobs/rollup.py" in files
    assert "__pycache__" not in files and ".pyc" not in files


# --- the token never touches the worktree -----------------------------------

TOKEN = "ghp_SECRETSECRETSECRET0123456789"


def test_the_token_is_not_written_into_the_worktree(origin: Path, monkeypatch) -> None:
    """It used to be embedded in the clone URL, which git persists in
    .git/config — readable by the agent with `cat .git/config`."""
    settings = Settings(dry_run=False, provider="gemini", github_token=TOKEN)

    with Worktree(
        RepoTarget(slug="org/repo", ref=None, remote_url=str(origin)), github_token=TOKEN
    ) as worktree:
        config = (worktree.path / ".git" / "config").read_text()
        assert TOKEN not in config and "x-access-token" not in config
        # Credentials travel per command, base64-encoded in a header, never stored.
        assert worktree.auth_args and TOKEN not in " ".join(worktree.auth_args)

        (worktree.path / "jobs" / "rollup.py").write_text("col = 'z'\n")
        url, _ = _open(worktree, settings, monkeypatch)

    assert url == "https://github.com/org/repo/pull/42"
    assert "incident-agent/" in _run("git", "branch", "--list", cwd=origin)


def test_git_errors_do_not_echo_the_auth_header(tmp_path: Path) -> None:
    with pytest.raises(GitError) as exc:
        git("-c", "http.extraheader=AUTHORIZATION: basic c2VjcmV0", "no-such-subcommand", cwd=tmp_path)
    assert "c2VjcmV0" not in str(exc.value) and "redacted" in str(exc.value)


# --- dedupe -----------------------------------------------------------------


def test_find_existing_pr_matches_on_the_branch_name(origin: Path, monkeypatch) -> None:
    """The fingerprint lives in the branch name. `gh pr list --search` looks at
    title and body, so the old implementation never found anything."""
    seen: list[list[str]] = []

    def fake_gh(args, cwd, token):
        seen.append(args)
        return 0, json.dumps(
            [
                {"url": "https://github.com/org/repo/pull/7",
                 "headRefName": "incident-agent/schema_mismatch-daily-rollup-abcd1234"},
                {"url": "https://github.com/org/repo/pull/8", "headRefName": "feature/x"},
            ]
        )

    monkeypatch.setattr("incident_agent.pr._gh", fake_gh)
    with Worktree(RepoTarget(slug="org/repo", ref=None, remote_url=str(origin))) as worktree:
        assert find_existing_pr(worktree, "abcd1234ffffffff", None) == (
            "https://github.com/org/repo/pull/7"
        )
        assert find_existing_pr(worktree, "0000000011111111", None) is None

    assert all("--search" not in args for args in seen)
    assert all("headRefName" in " ".join(args) for args in seen)


def test_find_existing_pr_survives_a_broken_gh(origin: Path, monkeypatch) -> None:
    monkeypatch.setattr("incident_agent.pr._gh", lambda a, c, t: (1, "gh: not logged in"))
    with Worktree(RepoTarget(slug="org/repo", ref=None, remote_url=str(origin))) as worktree:
        assert find_existing_pr(worktree, "abcd1234", None) is None


# --- a branch left behind by a dead run ---------------------------------------


def test_a_branch_left_by_a_dead_run_is_replaced(origin: Path, monkeypatch) -> None:
    """Push succeeded, `gh pr create` died: the deterministic branch exists on
    the remote with no PR. The next attempt used to be rejected as
    non-fast-forward, forever."""
    settings = Settings(dry_run=False, provider="gemini")
    target = RepoTarget(slug="org/repo", ref=None, remote_url=str(origin))

    with Worktree(target) as worktree:
        (worktree.path / "jobs" / "rollup.py").write_text("col = 'first attempt'\n")
        _open(worktree, settings, monkeypatch)

    with Worktree(target) as worktree:
        (worktree.path / "jobs" / "rollup.py").write_text("col = 'second attempt'\n")
        url, _ = _open(worktree, settings, monkeypatch)

    assert url
    branches = [
        line.strip().lstrip("* ")
        for line in _run("git", "branch", "--list", cwd=origin).splitlines()
        if "incident-agent/" in line
    ]
    assert len(branches) == 1, branches
    content = _run("git", "show", f"{branches[0]}:jobs/rollup.py", cwd=origin)
    assert content.strip() == "col = 'second attempt'"
