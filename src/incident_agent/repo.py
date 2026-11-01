"""Resolve a failing job to a repository checkout, and own all git operations.

The fix agent is blocked from running git (see :mod:`guardrails`); every write
to version control happens here, after the diff has been produced and inspected.

The GitHub token never touches the worktree. It is not put in the clone URL —
git would persist that in ``.git/config``, where the agent could read it back —
but supplied per command as an ``http.extraheader`` that is never stored.
"""

from __future__ import annotations

import base64
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import structlog
import yaml

from .models import FailureEvent

log = structlog.get_logger(__name__)


class GitError(RuntimeError):
    pass


def git(*args: str, cwd: Path, check: bool = True, timeout: int = 300) -> str:
    """Run a git command and return stdout."""
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        shown = " ".join(_mask_auth(a) for a in args)
        raise GitError(f"git {shown} failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout


def _mask_auth(arg: str) -> str:
    return "http.extraheader=AUTHORIZATION: «redacted»" if arg.startswith("http.extraheader=") else arg


def auth_args(token: str | None) -> list[str]:
    """``git -c`` arguments that authenticate one command without persisting anything.

    Placed *before* the subcommand (``git -c ... clone``) the setting applies to
    that invocation only; ``git clone -c ...`` would write it into the new
    repository's config.
    """
    if not token:
        return []
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return ["-c", f"http.extraheader=AUTHORIZATION: basic {basic}"]


@dataclass(slots=True)
class RepoTarget:
    """Where the failing job's code lives."""

    #: ``org/name`` on GitHub, or ``None`` when using a local path.
    slug: str | None
    #: Commit or branch to check out. ``None`` means the default branch.
    ref: str | None = None
    #: Set when the caller supplied an existing checkout instead of a slug.
    local_path: Path | None = None
    #: Clone from this URL instead of deriving one from ``slug``. Lets the push
    #: path be exercised against a local bare repository in tests, and supports
    #: mirrors or GitHub Enterprise without special-casing.
    remote_url: str | None = None

    @property
    def describe(self) -> str:
        if self.local_path:
            return str(self.local_path)
        return f"{self.slug}@{self.ref or 'default'}"


def resolve_target(
    event: FailureEvent,
    *,
    registry_path: Path | None = None,
    local_repo: str | Path | None = None,
) -> RepoTarget | None:
    """Map a failure to a repo.

    Resolution order: explicit local path, then Dataproc batch labels, then the
    job registry. Returns ``None`` when the job cannot be mapped — the caller
    degrades to a diagnosis-only notification rather than guessing.
    """
    if local_repo:
        return RepoTarget(slug=None, local_path=Path(local_repo).resolve())

    labels = {k.lower(): v for k, v in event.labels.items()}
    slug = labels.get("repo") or labels.get("repository")
    if slug:
        ref = labels.get("git-sha") or labels.get("git_sha") or labels.get("commit")
        log.info("repo_resolved", via="labels", slug=slug, ref=ref)
        return RepoTarget(slug=slug.replace("--", "/"), ref=ref)

    if registry_path and registry_path.exists():
        registry = yaml.safe_load(registry_path.read_text()) or {}
        for prefix, entry in (registry.get("jobs") or {}).items():
            if event.job_name.startswith(prefix) or event.batch_id.startswith(prefix):
                log.info("repo_resolved", via="registry", prefix=prefix, slug=entry.get("repo"))
                return RepoTarget(slug=entry.get("repo"), ref=entry.get("ref"))

    log.warning("repo_unresolved", job=event.job_name, batch=event.batch_id)
    return None


class Worktree:
    """A disposable checkout the agent may modify.

    For a remote slug this is a shallow clone. For a local path it is a *copy*,
    so a failed run never leaves the user's working tree dirty.
    """

    def __init__(self, target: RepoTarget, *, github_token: str | None = None) -> None:
        self.target = target
        self.github_token = github_token
        self.path: Path | None = None
        self._tmp: str | None = None
        self.base_ref: str | None = None

    def __enter__(self) -> Worktree:
        self._tmp = tempfile.mkdtemp(prefix="incident-agent-")
        dest = Path(self._tmp) / "repo"

        if self.target.local_path:
            shutil.copytree(
                self.target.local_path,
                dest,
                symlinks=True,
                ignore=shutil.ignore_patterns(
                    "__pycache__", "*.pyc", ".venv", "venv", ".pytest_cache", "node_modules"
                ),
            )
            if not (dest / ".git").exists():
                git("init", "-q", cwd=dest)
            _exclude_build_artifacts(dest)
            # Commit whatever the copy contains — including any changes the user
            # had uncommitted — so the diff we later produce holds only the
            # agent's edits, never the user's work in progress.
            git("add", "-A", cwd=dest)
            if git("status", "--porcelain", cwd=dest).strip():
                git(
                    "-c", "user.email=agent@local", "-c", "user.name=incident-agent",
                    "commit", "-q", "-m", "incident-agent baseline", cwd=dest,
                )
        else:
            url = self._clone_url()
            args = [*self.auth_args, "clone", "--depth", "50", "--quiet"]
            if self.target.ref and not _looks_like_sha(self.target.ref):
                args += ["--branch", self.target.ref]
            git(*args, url, str(dest), cwd=Path(self._tmp))
            if self.target.ref and _looks_like_sha(self.target.ref):
                git("checkout", "--quiet", self.target.ref, cwd=dest)
            _exclude_build_artifacts(dest)

        self.path = dest
        self.base_ref = git("rev-parse", "HEAD", cwd=dest).strip()
        log.info("worktree_ready", path=str(dest), base=self.base_ref[:12])
        return self

    def __exit__(self, *exc) -> None:
        if self._tmp and Path(self._tmp).exists():
            shutil.rmtree(self._tmp, ignore_errors=True)
        self.path = None

    @property
    def auth_args(self) -> list[str]:
        """Per-command credentials for clone, ls-remote and push."""
        return auth_args(self.github_token)

    def _clone_url(self) -> str:
        """The remote URL — credential-free, since git stores it in .git/config."""
        if self.target.remote_url:
            return self.target.remote_url
        if not self.target.slug:
            raise GitError("no repository slug to clone")
        return f"https://github.com/{self.target.slug}.git"

    # --- inspection ---

    def diff(self) -> str:
        assert self.path is not None
        git("add", "-A", cwd=self.path)
        return git("diff", "--cached", cwd=self.path)

    def changed_files(self) -> list[str]:
        assert self.path is not None
        git("add", "-A", cwd=self.path)
        out = git("diff", "--cached", "--name-only", cwd=self.path)
        return [line for line in out.splitlines() if line.strip()]

    def is_dirty(self) -> bool:
        return bool(self.changed_files())

    def reset(self) -> None:
        """Throw away agent edits — used between failed fix iterations."""
        assert self.path is not None
        git("reset", "--hard", "--quiet", "HEAD", cwd=self.path)
        git("clean", "-fdq", cwd=self.path)


#: Artifacts produced by running the verification step. Excluded locally via
#: `.git/info/exclude` rather than `.gitignore`, so the repo's own ignore rules
#: are untouched and the exclusion never appears in the PR diff.
_BUILD_ARTIFACTS = [
    "__pycache__/",
    "*.py[cod]",
    ".pytest_cache/",
    ".ruff_cache/",
    ".mypy_cache/",
    "*.egg-info/",
    ".coverage",
    "htmlcov/",
    "spark-warehouse/",
    "metastore_db/",
    "derby.log",
]


def _exclude_build_artifacts(repo: Path) -> None:
    exclude_file = repo / ".git" / "info" / "exclude"
    exclude_file.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude_file.read_text() if exclude_file.exists() else ""
    additions = [p for p in _BUILD_ARTIFACTS if p not in existing]
    if additions:
        with exclude_file.open("a") as fh:
            fh.write("\n# added by incident-agent\n" + "\n".join(additions) + "\n")


def _looks_like_sha(ref: str) -> bool:
    return len(ref) >= 7 and all(c in "0123456789abcdef" for c in ref.lower())
