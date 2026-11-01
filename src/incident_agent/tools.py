"""Repository tools for the Gemini agent loop.

The Claude Agent SDK supplied these; on Gemini we implement them, which means
the guardrails move from a ``PreToolUse`` hook into the dispatcher. The checks
themselves are unchanged — :mod:`guardrails` is pure functions, so the same
tested boundary applies to both providers.

Every dispatch is checked. There is no path from a model-supplied argument to
the filesystem that skips :func:`check_read_path`, :func:`check_write_path` or
:func:`check_bash_command`.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from .guardrails import check_bash_command, check_read_path, check_write_path, subprocess_env

log = structlog.get_logger(__name__)

MAX_READ_BYTES = 120_000
MAX_TOOL_OUTPUT = 30_000
BASH_TIMEOUT_S = 900

#: Directories never worth walking for grep/glob.
SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", "node_modules", "target",
    ".pytest_cache", ".ruff_cache", ".mypy_cache", "build", "dist",
}


def _truncate(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text) - limit:,} more characters]"


@dataclass
class RepoTools:
    """Filesystem tools bound to one worktree, with guardrails enforced."""

    worktree: Path
    editable_globs: list[str] = field(default_factory=list)
    protected_globs: list[str] = field(default_factory=list)
    #: Root-cause stage: expose only Read/Grep/Glob.
    read_only: bool = True
    violations: list[str] = field(default_factory=list)

    # --- declarations -----------------------------------------------------

    def declarations(self) -> list[dict[str, Any]]:
        """Gemini FunctionDeclaration dicts for the enabled tools."""
        decls: list[dict[str, Any]] = [
            {
                "name": "read_file",
                "description": (
                    "Read a text file from the repository. Returns the content with "
                    "1-based line numbers."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "path": {
                            "type": "STRING",
                            "description": "Repository-relative path, e.g. jobs/rollup.py",
                        },
                        "start_line": {
                            "type": "INTEGER",
                            "description": "Optional 1-based first line to return.",
                        },
                        "end_line": {"type": "INTEGER", "description": "Optional last line."},
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "grep",
                "description": (
                    "Search file contents with a regular expression. Returns matching "
                    "lines prefixed with path:line."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "pattern": {"type": "STRING", "description": "Python regex."},
                        "path": {
                            "type": "STRING",
                            "description": "Optional subdirectory to search under.",
                        },
                        "file_glob": {
                            "type": "STRING",
                            "description": "Optional filename filter, e.g. *.scala",
                        },
                    },
                    "required": ["pattern"],
                },
            },
            {
                "name": "glob",
                "description": "List files matching a glob pattern, e.g. src/**/*.scala",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {"pattern": {"type": "STRING"}},
                    "required": ["pattern"],
                },
            },
        ]

        if self.read_only:
            return decls

        decls += [
            {
                "name": "edit_file",
                "description": (
                    "Replace an exact string in a file. old_string must appear exactly "
                    "once. This is the preferred way to change code."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "path": {"type": "STRING"},
                        "old_string": {
                            "type": "STRING",
                            "description": "Exact text to replace, including indentation.",
                        },
                        "new_string": {"type": "STRING", "description": "Replacement text."},
                    },
                    "required": ["path", "old_string", "new_string"],
                },
            },
            {
                "name": "write_file",
                "description": "Create or overwrite a file with the given content.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "path": {"type": "STRING"},
                        "content": {"type": "STRING"},
                    },
                    "required": ["path", "content"],
                },
            },
            {
                "name": "run_command",
                "description": (
                    "Run a shell command in the repository root, for example to run the "
                    "test suite. Network, git and package-install commands are refused."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {"command": {"type": "STRING"}},
                    "required": ["command"],
                },
            },
        ]
        return decls

    # --- dispatch ---------------------------------------------------------

    def dispatch(self, name: str, args: dict[str, Any]) -> str:
        """Execute a tool call. Never raises — errors come back as text."""
        handler = {
            "read_file": self._read_file,
            "grep": self._grep,
            "glob": self._glob,
            "edit_file": self._edit_file,
            "write_file": self._write_file,
            "run_command": self._run_command,
        }.get(name)

        if handler is None:
            return f"ERROR: unknown tool {name!r}"
        if self.read_only and name in {"edit_file", "write_file", "run_command"}:
            return self._deny(name, "this stage has read-only access to the repository")

        try:
            return _truncate(handler(args))
        except Exception as exc:  # noqa: BLE001 - a tool error must not kill the run
            log.warning("tool_error", tool=name, error=str(exc)[:200])
            return f"ERROR: {type(exc).__name__}: {exc}"

    def _deny(self, tool: str, reason: str) -> str:
        detail = f"{tool}: {reason}"
        self.violations.append(detail)
        log.warning("guardrail_denied", tool=tool, reason=reason)
        return f"DENIED: {reason}"

    # --- read-side --------------------------------------------------------

    def _resolve_readable(self, raw: str) -> Path | str:
        if reason := check_read_path(raw, worktree=self.worktree):
            return self._deny("read", reason)
        candidate = Path(raw)
        return candidate if candidate.is_absolute() else self.worktree / candidate

    def _read_file(self, args: dict[str, Any]) -> str:
        resolved = self._resolve_readable(str(args.get("path", "")))
        if isinstance(resolved, str):
            return resolved
        if not resolved.is_file():
            return f"ERROR: no such file: {args.get('path')}"

        raw = resolved.read_bytes()[:MAX_READ_BYTES].decode("utf-8", errors="replace")
        lines = raw.splitlines()

        start = max(1, int(args.get("start_line") or 1))
        end = min(len(lines), int(args.get("end_line") or len(lines)))
        selected = lines[start - 1 : end]

        return "\n".join(f"{i:>5} | {line}" for i, line in enumerate(selected, start=start))

    def _iter_files(self, root: Path, file_glob: str | None = None):
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if file_glob and not path.match(file_glob):
                continue
            yield path

    def _grep(self, args: dict[str, Any]) -> str:
        pattern = str(args.get("pattern", ""))
        if not pattern:
            return "ERROR: pattern is required"
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return f"ERROR: invalid regex: {exc}"

        root = self.worktree
        if sub := args.get("path"):
            resolved = self._resolve_readable(str(sub))
            if isinstance(resolved, str):
                return resolved
            root = resolved
        if not root.exists():
            return f"ERROR: no such path: {args.get('path')}"
        if root.is_file():
            root_files = [root]
        else:
            root_files = list(self._iter_files(root, args.get("file_glob")))

        hits: list[str] = []
        for path in root_files:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for n, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    rel = path.relative_to(self.worktree).as_posix()
                    hits.append(f"{rel}:{n}: {line.strip()[:200]}")
                    if len(hits) >= 300:
                        hits.append("... [more matches suppressed]")
                        return "\n".join(hits)
        return "\n".join(hits) if hits else "(no matches)"

    def _glob(self, args: dict[str, Any]) -> str:
        pattern = str(args.get("pattern", "*"))
        found = [
            p.relative_to(self.worktree).as_posix()
            for p in self.worktree.glob(pattern)
            if p.is_file() and not any(part in SKIP_DIRS for part in p.parts)
        ]
        return "\n".join(sorted(found)[:500]) if found else "(no files matched)"

    # --- write-side -------------------------------------------------------

    def _resolve_writable(self, raw: str, tool: str) -> Path | str:
        reason = check_write_path(
            raw,
            worktree=self.worktree,
            editable_globs=self.editable_globs,
            protected_globs=self.protected_globs,
        )
        if reason:
            return self._deny(tool, reason)
        candidate = Path(raw)
        return candidate if candidate.is_absolute() else self.worktree / candidate

    def _edit_file(self, args: dict[str, Any]) -> str:
        resolved = self._resolve_writable(str(args.get("path", "")), "edit_file")
        if isinstance(resolved, str):
            return resolved
        if not resolved.is_file():
            return f"ERROR: no such file: {args.get('path')}"

        old = str(args.get("old_string", ""))
        new = str(args.get("new_string", ""))
        if not old:
            return "ERROR: old_string is required and must not be empty"

        text = resolved.read_text(encoding="utf-8")
        count = text.count(old)
        if count == 0:
            return (
                "ERROR: old_string was not found. Read the file again and copy the "
                "exact text, including indentation."
            )
        if count > 1:
            return (
                f"ERROR: old_string appears {count} times; it must be unique. "
                "Include more surrounding context to disambiguate."
            )

        resolved.write_text(text.replace(old, new), encoding="utf-8")
        rel = resolved.relative_to(self.worktree).as_posix()
        log.info("tool_edit", path=rel)
        return f"Edited {rel}."

    def _write_file(self, args: dict[str, Any]) -> str:
        resolved = self._resolve_writable(str(args.get("path", "")), "write_file")
        if isinstance(resolved, str):
            return resolved
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(str(args.get("content", "")), encoding="utf-8")
        rel = resolved.relative_to(self.worktree).as_posix()
        log.info("tool_write", path=rel)
        return f"Wrote {rel}."

    def _run_command(self, args: dict[str, Any]) -> str:
        command = str(args.get("command", ""))
        if reason := check_bash_command(command):
            return self._deny("run_command", reason)

        log.info("tool_bash", command=command[:160])
        try:
            proc = subprocess.run(
                ["/bin/bash", "-c", command],
                cwd=str(self.worktree),
                capture_output=True,
                text=True,
                timeout=BASH_TIMEOUT_S,
                # Same allow-list as the verification step: no tokens, no keys.
                env=subprocess_env(home=self.worktree),
            )
        except subprocess.TimeoutExpired:
            return f"ERROR: command timed out after {BASH_TIMEOUT_S}s"

        output = (proc.stdout or "") + (proc.stderr or "")
        return f"exit={proc.returncode}\n{output.strip()}"
