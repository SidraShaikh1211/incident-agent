"""The Gemini tool layer, where the guardrails now live.

On the Claude provider a `PreToolUse` hook vetoed tool calls. On Gemini the
tools are ours, so enforcement moved into `RepoTools.dispatch`. The underlying
checks are the same pure functions — these tests prove the *dispatcher* applies
them, which is the part that is new.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from incident_agent.tools import RepoTools

EDITABLE = ["src/**", "jobs/**", "*.py"]
PROTECTED = [".git/**", "**/*.tf", "**/.env*", "**/secrets/**"]


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    files = {
        "jobs/rollup.py": "def rollup(df):\n    return df.withColumn('d', col('event_ts'))\n",
        "src/util.py": "X = 1\n",
        "tests/test_rollup.py": "def test_ok():\n    assert True\n",
        "infra/main.tf": 'resource "google_storage_bucket" "b" {}\n',
        ".env": "SECRET=hunter2\n",
        "secrets/key.json": "{}\n",
        "README.md": "# docs\n",
    }
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("x\n")
    return tmp_path


def rw(repo: Path, violations: list[str] | None = None) -> RepoTools:
    return RepoTools(
        worktree=repo,
        editable_globs=EDITABLE,
        protected_globs=PROTECTED,
        read_only=False,
        violations=violations if violations is not None else [],
    )


def ro(repo: Path, violations: list[str] | None = None) -> RepoTools:
    return RepoTools(
        worktree=repo,
        editable_globs=[],
        protected_globs=PROTECTED,
        read_only=True,
        violations=violations if violations is not None else [],
    )


# --- read-only stage really is read-only ------------------------------------


@pytest.mark.parametrize("tool", ["edit_file", "write_file", "run_command"])
def test_read_only_stage_refuses_mutating_tools(repo: Path, tool: str) -> None:
    violations: list[str] = []
    out = ro(repo, violations).dispatch(tool, {"path": "jobs/rollup.py", "command": "ls"})
    assert out.startswith("DENIED")
    assert "read-only" in out
    assert violations


def test_read_only_stage_does_not_declare_mutating_tools(repo: Path) -> None:
    """The model should not even be offered a tool it cannot use."""
    names = {d["name"] for d in ro(repo).declarations()}
    assert names == {"read_file", "grep", "glob"}


def test_fix_stage_declares_the_full_set(repo: Path) -> None:
    names = {d["name"] for d in rw(repo).declarations()}
    assert names == {"read_file", "grep", "glob", "edit_file", "write_file", "run_command"}


# --- reads ------------------------------------------------------------------


def test_read_returns_numbered_lines(repo: Path) -> None:
    out = ro(repo).dispatch("read_file", {"path": "jobs/rollup.py"})
    assert "1 |" in out and "event_ts" in out


def test_read_respects_a_line_range(repo: Path) -> None:
    out = ro(repo).dispatch("read_file", {"path": "jobs/rollup.py", "start_line": 2, "end_line": 2})
    assert "event_ts" in out
    assert "def rollup" not in out


@pytest.mark.parametrize("path", ["../../../etc/passwd", "/etc/passwd"])
def test_read_cannot_escape_the_worktree(repo: Path, path: str) -> None:
    out = ro(repo).dispatch("read_file", {"path": path})
    assert out.startswith("DENIED") and "escapes" in out


@pytest.mark.parametrize("path", [".env", "secrets/key.json"])
def test_read_cannot_exfiltrate_credentials(repo: Path, path: str) -> None:
    """Reading a checked-in secret is an exfiltration path even without writes."""
    out = ro(repo).dispatch("read_file", {"path": path})
    assert out.startswith("DENIED") and "credentials" in out
    assert "hunter2" not in out


def test_read_may_open_files_it_cannot_edit(repo: Path) -> None:
    """Tests and build files inform the diagnosis but are out of edit scope."""
    out = ro(repo).dispatch("read_file", {"path": "tests/test_rollup.py"})
    assert "def test_ok" in out


# --- search -----------------------------------------------------------------


def test_grep_finds_matches_with_paths_and_lines(repo: Path) -> None:
    out = ro(repo).dispatch("grep", {"pattern": r"event_ts"})
    assert "jobs/rollup.py:2:" in out


def test_grep_reports_no_matches_clearly(repo: Path) -> None:
    assert ro(repo).dispatch("grep", {"pattern": "zzz_nope"}) == "(no matches)"


def test_grep_rejects_a_bad_regex(repo: Path) -> None:
    assert "invalid regex" in ro(repo).dispatch("grep", {"pattern": "([unclosed"})


def test_grep_skips_vendored_directories(repo: Path) -> None:
    (repo / ".venv" / "lib").mkdir(parents=True)
    (repo / ".venv" / "lib" / "x.py").write_text("event_ts\n")
    out = ro(repo).dispatch("grep", {"pattern": "event_ts"})
    assert ".venv" not in out


def test_glob_lists_matching_files(repo: Path) -> None:
    out = ro(repo).dispatch("glob", {"pattern": "jobs/*.py"})
    assert out.strip() == "jobs/rollup.py"


# --- writes -----------------------------------------------------------------


def test_edit_applies_a_unique_replacement(repo: Path) -> None:
    out = rw(repo).dispatch(
        "edit_file",
        {"path": "jobs/rollup.py", "old_string": "event_ts", "new_string": "event_timestamp"},
    )
    assert out.startswith("Edited")
    assert "event_timestamp" in (repo / "jobs/rollup.py").read_text()


def test_edit_refuses_an_ambiguous_match(repo: Path) -> None:
    (repo / "jobs/dup.py").write_text("a = 1\na = 1\n")
    out = rw(repo).dispatch(
        "edit_file", {"path": "jobs/dup.py", "old_string": "a = 1", "new_string": "a = 2"}
    )
    assert "appears 2 times" in out
    assert (repo / "jobs/dup.py").read_text() == "a = 1\na = 1\n"


def test_edit_reports_a_missing_match_actionably(repo: Path) -> None:
    out = rw(repo).dispatch(
        "edit_file", {"path": "jobs/rollup.py", "old_string": "nope", "new_string": "x"}
    )
    assert "not found" in out


@pytest.mark.parametrize("path", ["infra/main.tf", ".env", "secrets/key.json", ".git/config"])
def test_write_refuses_protected_paths(repo: Path, path: str) -> None:
    violations: list[str] = []
    out = rw(repo, violations).dispatch("write_file", {"path": path, "content": "pwned"})
    assert out.startswith("DENIED")
    assert violations
    assert "pwned" not in (repo / path).read_text()


def test_write_refuses_paths_outside_the_editable_scope(repo: Path) -> None:
    out = rw(repo).dispatch("write_file", {"path": "README.md", "content": "x"})
    assert out.startswith("DENIED") and "editable scope" in out


def test_write_refuses_traversal(repo: Path, tmp_path: Path) -> None:
    out = rw(repo).dispatch("write_file", {"path": "../escaped.py", "content": "x"})
    assert out.startswith("DENIED")
    assert not (tmp_path.parent / "escaped.py").exists()


def test_write_creates_parent_directories_in_scope(repo: Path) -> None:
    out = rw(repo).dispatch("write_file", {"path": "src/deep/new.py", "content": "Y = 2\n"})
    assert out.startswith("Wrote")
    assert (repo / "src/deep/new.py").read_text() == "Y = 2\n"


# --- commands ---------------------------------------------------------------


def test_run_command_executes_and_reports_exit_code(repo: Path) -> None:
    out = rw(repo).dispatch("run_command", {"command": "echo hello"})
    assert "exit=0" in out and "hello" in out


@pytest.mark.parametrize(
    "command",
    ["git push", "gh pr create", "curl http://x", "rm -rf .", "pip install requests",
     "pytest && git push", "echo $(git push)"],
)
def test_run_command_refuses_dangerous_commands(repo: Path, command: str) -> None:
    violations: list[str] = []
    out = rw(repo, violations).dispatch("run_command", {"command": command})
    assert out.startswith("DENIED"), f"{command!r} was allowed"
    assert violations


def test_run_command_runs_in_the_worktree(repo: Path) -> None:
    out = rw(repo).dispatch("run_command", {"command": "pwd"})
    assert str(repo.resolve()) in out


# --- dispatcher behaviour ---------------------------------------------------


def test_unknown_tool_is_reported_not_raised(repo: Path) -> None:
    assert "unknown tool" in rw(repo).dispatch("delete_everything", {})


def test_tool_errors_come_back_as_text(repo: Path) -> None:
    """A tool exception must not abort the incident run."""
    out = rw(repo).dispatch("read_file", {"path": "does/not/exist.py"})
    assert out.startswith("ERROR") and "no such file" in out


# --- .git is off limits -----------------------------------------------------


@pytest.mark.parametrize("path", [".git/config", ".git/HEAD"])
def test_read_cannot_open_git_internals(repo: Path, path: str) -> None:
    """The remote URL lives in .git/config. Even without a token in it, the
    agent has no business there — and historically there *was* a token in it."""
    violations: list[str] = []
    out = ro(repo, violations).dispatch("read_file", {"path": path})
    assert out.startswith("DENIED") and violations


def test_run_command_env_has_no_secrets(repo: Path, monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_0123456789abcdefghij")
    out = rw(repo).dispatch("run_command", {"command": "env"})
    assert "exit=0" in out
    assert "ghp_0123456789abcdefghij" not in out and "GITHUB_TOKEN" not in out
