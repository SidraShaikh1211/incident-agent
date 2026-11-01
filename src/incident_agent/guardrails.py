"""Enforcement layer for the fix agent.

Prompt instructions are advisory. This module is not: it runs as a ``PreToolUse``
hook, which fires for *every* tool call regardless of permission mode, and can
veto the call before it executes.

Three rules:

* File reads must resolve inside the worktree and may not open credential
  files or anything under ``.git/`` — the remote URL and any cached auth live
  there.
* File writes must land inside the worktree, match an allowed glob, and miss
  every protected glob.
* Bash may not run git, network, cloud or destructive commands. The agent never
  touches git — the orchestrator commits and pushes after inspecting the diff.

What this does *not* do: inspect the arguments of interpreters. ``python -c``
can import ``urllib`` or ``subprocess``, so the bash deny-list is a tripwire,
not a sandbox. The real containment is that the agent's subprocess environment
carries no credentials (see :func:`subprocess_env`) and the worktree holds none
(see :mod:`incident_agent.repo`).
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

WRITE_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}
#: Tools that read the filesystem. Their path argument must stay in the worktree.
READ_TOOLS = {"Read", "Grep", "Glob", "LS", "NotebookRead"}

#: Paths that may never be read, even inside the worktree. ``.git/`` is here
#: because the remote URL and any credential helper state live in it.
UNREADABLE_GLOBS = [
    "**/.git/**",
    "**/.env*",
    "**/secrets/**",
    "**/credentials*",
    "**/id_rsa*",
    "**/*.pem",
    "**/*.key",
]

#: Environment variables a verification or agent subprocess may inherit.
#: Everything else — tokens, API keys, ADC paths — is dropped. Target-repo test
#: code runs with this env, so it must not see the orchestrator's secrets.
_INHERITED_ENV = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TMPDIR",
    "JAVA_HOME",
    "SPARK_HOME",
    "HADOOP_HOME",
    "PYSPARK_PYTHON",
    "PYSPARK_DRIVER_PYTHON",
    "VIRTUAL_ENV",
)


def subprocess_env(*, home: Path) -> dict[str, str]:
    """A minimal environment for anything that executes repository code.

    Used by the agent's ``run_command`` tool and by the verification step. The
    same allow-list in both places means a test the agent writes cannot see
    anything its shell could not.
    """
    env = {k: os.environ[k] for k in _INHERITED_ENV if os.environ.get(k)}
    env.update(
        {
            "HOME": str(home),
            "GIT_TERMINAL_PROMPT": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return env

#: Command names the agent may never invoke, whatever the arguments.
_BANNED_COMMANDS = {
    "git",  # the orchestrator owns version control
    "gh",
    "curl",
    "wget",
    "nc",
    "ncat",
    "ssh",
    "scp",
    "rsync",
    "sudo",
    "su",
    "doas",
    "gcloud",
    "gsutil",
    "bq",
    "aws",
    "az",
    "kubectl",
    "docker",
    "terraform",
    "systemctl",
    "shutdown",
    "reboot",
    "mkfs",
    "dd",
}

#: Package installs would mutate the environment the verification step trusts.
_BANNED_INSTALL = re.compile(r"^(?:pip3?|uv|poetry|npm|pnpm|yarn|apt|apt-get|brew|conda)$")

#: Patterns checked against the raw command string.
_BANNED_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("recursive delete", re.compile(r"\brm\b[^|;&]*\s-[a-zA-Z]*[rR][a-zA-Z]*\b")),
    ("write to device", re.compile(r">\s*/dev/(?!null\b)")),
    ("write outside worktree", re.compile(r">\s*/(?:etc|usr|bin|sbin|var|opt|System)\b")),
    ("history rewrite", re.compile(r"\bgit\b.*\b(?:push|reset\s+--hard|rebase|filter-branch)\b")),
    (
        "credential file access",
        re.compile(r"(?:\.env\b|id_rsa|\.aws/credentials|\.netrc|\.ssh/|\.git/|git-credentials)"),
    ),
    ("fork bomb", re.compile(r":\(\)\s*\{.*\|.*&.*\}")),
    ("reverse shell", re.compile(r"/dev/tcp/")),
]


class GuardrailViolation(Exception):
    """Raised by the standalone checkers when a call must be refused."""


# --- glob matching ----------------------------------------------------------


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Compile a gitignore-style glob.

    ``**`` crosses directory separators, ``*`` and ``?`` do not.
    """
    out: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "*":
            if pattern.startswith("**", i):
                # '**/' collapses to "zero or more directories"
                if pattern.startswith("**/", i):
                    out.append("(?:.*/)?")
                    i += 3
                    continue
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def matches_any(rel_path: str, patterns: list[str]) -> bool:
    """Whether ``rel_path`` (posix, worktree-relative) matches any glob."""
    return any(_glob_to_regex(p).match(rel_path) for p in patterns)


def check_read_path(raw_path: str | None, *, worktree: Path) -> str | None:
    """Containment check for reads. Returns a denial reason, or ``None``.

    Reads do not need to match the editable globs — the root-cause agent must
    be able to look at build files and tests it may not modify — but they must
    still resolve inside the worktree. Without this an agent could read
    ``/etc/passwd`` or the host's credentials through a relative path.
    """
    if not raw_path:
        return "the tool call did not specify a file path"

    worktree = worktree.resolve()
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = worktree / candidate

    resolved = candidate.resolve()
    try:
        resolved.relative_to(worktree)
    except ValueError:
        return f"path escapes the repository worktree (resolved to {resolved})"

    rel = resolved.relative_to(worktree).as_posix()
    # Reading a checked-in secret is still an exfiltration path, and .git/
    # holds the remote URL plus whatever a credential helper cached.
    if matches_any(rel, UNREADABLE_GLOBS):
        return f"'{rel}' holds credentials and may not be read"
    return None


def check_write_path(
    raw_path: str | None,
    *,
    worktree: Path,
    editable_globs: list[str],
    protected_globs: list[str],
) -> str | None:
    """Return a denial reason, or ``None`` if the write is permitted."""
    if not raw_path:
        return "the tool call did not specify a file path"

    worktree = worktree.resolve()
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = worktree / candidate

    # Resolve symlinks and '..' before the containment check, so neither can be
    # used to escape the worktree.
    resolved = candidate.resolve()
    try:
        rel = resolved.relative_to(worktree).as_posix()
    except ValueError:
        return (
            f"path escapes the repository worktree "
            f"(resolved to {resolved}, worktree is {worktree})"
        )

    if matches_any(rel, protected_globs):
        return f"'{rel}' is a protected path and may never be modified"

    if not matches_any(rel, editable_globs):
        return (
            f"'{rel}' is outside the editable scope for this incident. "
            f"Allowed: {', '.join(editable_globs)}"
        )

    return None


# --- bash checking ----------------------------------------------------------


#: Shell operators that separate one command from the next.
_OPERATORS = {"|", "||", "&&", ";", "&", "\n", "(", ")", "{", "}"}


def _split_segments(command: str) -> list[list[str]] | None:
    """Tokenise a shell line and split it into per-command argv lists.

    Tokenising *before* splitting matters: a naive ``re.split`` on ``|`` cuts
    through quoted strings — ``grep -n "a|b" f`` would yield the fragment
    ``grep -n "a``, whose unbalanced quote then fails to parse and gets denied.
    ``shlex`` with ``punctuation_chars`` respects quoting and emits operators as
    their own tokens.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return None

    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in _OPERATORS:
            if current:
                segments.append(current)
                current = []
        else:
            current.append(token)
    if current:
        segments.append(current)
    return segments


def check_bash_command(command: str | None) -> str | None:
    """Return a denial reason, or ``None`` if the command is permitted."""
    if not command or not command.strip():
        return "empty bash command"

    for label, pattern in _BANNED_PATTERNS:
        if pattern.search(command):
            return f"command matches a blocked pattern ({label})"

    # Command substitution can smuggle a banned command past segment splitting.
    if re.search(r"\$\(|\breval\b|`", command):
        return "command substitution and eval are not permitted"

    segments = _split_segments(command)
    if segments is None:
        return "command could not be parsed as a shell command"

    for tokens in segments:
        if not tokens:
            continue

        # Step over env-var assignments and simple prefixes: FOO=bar cmd ...
        idx = 0
        while idx < len(tokens) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[idx]):
            idx += 1
        if idx >= len(tokens):
            continue

        argv0 = Path(tokens[idx]).name
        if argv0 in _BANNED_COMMANDS:
            return f"'{argv0}' is not available to the agent"
        if _BANNED_INSTALL.match(argv0) and len(tokens) > idx + 1:
            sub = tokens[idx + 1]
            if sub in {"install", "add", "uninstall", "remove", "sync", "upgrade"}:
                return (
                    f"'{argv0} {sub}' would mutate the environment; "
                    "the toolchain is fixed for this run"
                )

    return None


# --- the hook itself --------------------------------------------------------


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def make_pretooluse_hook(
    *,
    worktree: Path,
    editable_globs: list[str],
    protected_globs: list[str],
    violations: list[str] | None = None,
):
    """Build the ``PreToolUse`` callback bound to one worktree.

    ``violations`` receives a line per denial so the run record can show what
    the agent attempted.
    """
    sink = violations if violations is not None else []

    async def hook(input_data: dict[str, Any], tool_use_id: str | None, context: Any):
        tool_name = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input") or {}

        reason: str | None = None
        if tool_name in WRITE_TOOLS:
            reason = check_write_path(
                tool_input.get("file_path") or tool_input.get("notebook_path"),
                worktree=worktree,
                editable_globs=editable_globs,
                protected_globs=protected_globs,
            )
        elif tool_name in READ_TOOLS:
            # Read/NotebookRead always name a file. Grep/Glob/LS take an optional
            # directory; absent, they search the cwd, which is the worktree.
            raw = (
                tool_input.get("file_path")
                or tool_input.get("notebook_path")
                or tool_input.get("path")
            )
            if raw or tool_name in {"Read", "NotebookRead"}:
                reason = check_read_path(raw, worktree=worktree)
        elif tool_name == "Bash":
            reason = check_bash_command(tool_input.get("command"))

        if reason:
            detail = f"{tool_name}: {reason}"
            sink.append(detail)
            log.warning("guardrail_denied", tool=tool_name, reason=reason)
            return _deny(reason)

        return {}

    return hook
