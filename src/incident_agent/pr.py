"""Branch, commit, push and open the draft PR.

All git and GitHub access lives here — the agent has none of it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import structlog
from jinja2 import Environment, StrictUndefined

from .config import PROMPTS_DIR, Settings
from .models import (
    FailureEvent,
    FixResult,
    PRNarrative,
    RootCauseReport,
    TriageVerdict,
    VerifyResult,
)
from .redact import redact
from .repo import Worktree, git

log = structlog.get_logger(__name__)


def render_pr_body(
    event: FailureEvent,
    verdict: TriageVerdict,
    fix: FixResult,
    verify: VerifyResult,
    violations: str = "",
    *,
    report: RootCauseReport | None = None,
    narrative: PRNarrative | None = None,
) -> str:
    env = Environment(undefined=StrictUndefined, trim_blocks=False, lstrip_blocks=False)
    template = env.from_string((PROMPTS_DIR / "pr_body.md.j2").read_text(encoding="utf-8"))
    body = template.render(
        event=event,
        verdict=verdict,
        fix=fix,
        verify=verify,
        violations=violations,
        report=report,
        narrative=narrative,
        attribution=attribution(fix.model),
    )
    # Belt and braces: the agent prose and the log excerpts both flow in here.
    return redact(body)


def branch_name(event: FailureEvent, verdict: TriageVerdict, prefix: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", event.job_name.lower()).strip("-")[:40]
    short = event.fingerprint()[:8]
    return f"{prefix}/{verdict.failure_class}-{slug}-{short}"


def attribution(model: str) -> str:
    """Co-author trailer naming the model that actually wrote the patch.

    Empty when the model is unknown — a wrong attribution is worse than none.
    """
    if not model:
        return ""
    email = (
        "noreply@anthropic.com"
        if model.startswith("claude")
        else "incident-agent@users.noreply.github.com"
    )
    return f"Co-Authored-By: {model} <{email}>"


def commit_message(event: FailureEvent, verdict: TriageVerdict, *, model: str = "") -> str:
    subject = f"fix({event.job_name}): {verdict.failure_class.replace('_', ' ')}"
    if len(subject) > 72:
        subject = subject[:69] + "..."
    body = (
        f"{subject}\n\n"
        f"{verdict.root_cause}\n\n"
        f"Batch: {event.batch_id}\n"
        f"Fingerprint: {event.fingerprint()}\n"
        f"Triage confidence: {verdict.confidence:.2f}\n"
    )
    if trailer := attribution(model):
        body += f"\n{trailer}\n"
    return body


def _gh(args: list[str], cwd: Path, token: str | None) -> tuple[int, str]:
    env = {**os.environ}
    if token:
        env["GH_TOKEN"] = token
    proc = subprocess.run(
        ["gh", *args], cwd=str(cwd), capture_output=True, text=True, env=env, timeout=180
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def find_existing_pr(worktree: Worktree, fingerprint: str, token: str | None) -> str | None:
    """Avoid opening a second PR for a failure that recurs nightly.

    The fingerprint is carried in the branch name (see :func:`branch_name`), so
    the match is on ``headRefName``. ``gh pr list --search`` would look at
    title and body instead and miss every one of our PRs.
    """
    if worktree.target.local_path or not worktree.path:
        return None
    code, out = _gh(
        ["pr", "list", "--state", "open", "--limit", "100", "--json", "url,headRefName"],
        worktree.path,
        token,
    )
    if code != 0:
        log.warning("pr_list_failed", output=out.strip()[:400])
        return None
    try:
        prs = json.loads(out or "[]")
    except json.JSONDecodeError:
        log.warning("pr_list_unparseable", output=out.strip()[:200])
        return None
    short = fingerprint[:8]
    for pr in prs:
        if short in (pr.get("headRefName") or ""):
            return pr.get("url")
    return None


def _remote_branch_exists(worktree: Worktree, branch: str) -> bool:
    assert worktree.path is not None
    out = git(
        *worktree.auth_args, "ls-remote", "--heads", "origin", branch,
        cwd=worktree.path, check=False,
    )
    return bool(out.strip())


def open_pull_request(
    worktree: Worktree,
    event: FailureEvent,
    verdict: TriageVerdict,
    fix: FixResult,
    verify_result: VerifyResult,
    settings: Settings,
    *,
    report: RootCauseReport | None = None,
    narrative: PRNarrative | None = None,
    violations: str = "",
) -> str | None:
    """Commit, push and open a draft PR. Returns the PR URL, or ``None`` in dry-run."""
    assert worktree.path is not None
    path = worktree.path

    branch = branch_name(event, verdict, settings.pr_branch_prefix)
    body = render_pr_body(
        event, verdict, fix, verify_result, violations, report=report, narrative=narrative
    )

    if settings.dry_run:
        log.info("dry_run_skipping_pr", branch=branch)
        print("\n" + "=" * 72)
        print(f"DRY RUN — would push branch: {branch}")
        print("=" * 72)
        print(body)
        print("=" * 72)
        print("--- diff ---")
        print(worktree.diff())
        return None

    git("checkout", "-q", "-b", branch, cwd=path)
    git("add", "-A", cwd=path)
    git(
        "-c", "user.email=incident-agent@users.noreply.github.com",
        "-c", "user.name=Spark Incident Agent",
        "commit", "-q", "-m", commit_message(event, verdict, model=fix.model),
        cwd=path,
    )

    if worktree.target.local_path:
        log.info("local_repo_no_push", branch=branch)
        print(body)
        return None

    # The branch name is deterministic per fingerprint. If an earlier run pushed
    # it but died before `gh pr create`, a plain push is rejected and every
    # retry fails the same way. The branch is ours (no open PR — the dedupe
    # check ran first), so replace it.
    push = [*worktree.auth_args, "push", "-q", "--set-upstream"]
    if _remote_branch_exists(worktree, branch):
        log.warning("remote_branch_exists_replacing", branch=branch)
        push.append("--force")
    git(*push, "origin", branch, cwd=path)

    title = narrative.title if narrative else _pr_title(event, verdict)
    args = ["pr", "create", "--title", title[:100], "--body", body, "--head", branch]
    if settings.draft_pr:
        args.append("--draft")

    code, out = _gh(args, path, settings.github_token)
    if code != 0:
        log.error("pr_create_failed", output=out.strip()[:800])
        raise RuntimeError(f"gh pr create failed: {out.strip()[:400]}")

    url = next((line.strip() for line in out.splitlines() if line.startswith("https://")), None)
    log.info("pr_opened", url=url, branch=branch)
    return url


def _pr_title(event: FailureEvent, verdict: TriageVerdict) -> str:
    title = f"fix({event.job_name}): {verdict.failure_class.replace('_', ' ')}"
    return title[:100]
