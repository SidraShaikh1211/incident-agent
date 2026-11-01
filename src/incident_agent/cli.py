"""Command-line entrypoint. This is also the Cloud Run Job entrypoint."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Annotated

import structlog
import typer

from .config import get_settings
from .models import AUTO_PR_CLASSES, RunRecord
from .sources.local import FileLogSource

app = typer.Typer(
    add_completion=False,
    help="Analyse failed Spark jobs, patch the cause, and open a draft PR.",
    no_args_is_help=True,
)


def _configure_logging(json_logs: bool, level: str = "INFO") -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=level)
    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
    ]
    processors.append(
        structlog.processors.JSONRenderer()
        if json_logs
        # The default rich traceback prints every frame's locals, which on an
        # error path includes the Settings object — tokens and all. Plain
        # tracebacks show the stack and nothing else.
        else structlog.dev.ConsoleRenderer(
            colors=sys.stderr.isatty(), exception_formatter=structlog.dev.plain_traceback
        )
    )
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level)),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def _check_auth(settings) -> None:
    """Confirm the chosen inference provider can actually authenticate.

    Vertex uses Application Default Credentials; the first-party path uses the
    `claude` CLI's own login or ANTHROPIC_API_KEY. Checking here turns an
    opaque mid-run failure into an actionable message.
    """
    log = structlog.get_logger(__name__)

    if settings.provider == "gemini":
        project = settings.resolved_vertex_project
        if not project:
            typer.secho(
                "Gemini needs a GCP project — set INCIDENT_VERTEX_PROJECT "
                "or INCIDENT_GCP_PROJECT.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(2)

        adc = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
        if not adc.exists() and not os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
            typer.secho(
                "Vertex needs Application Default Credentials, which are not set up.\n"
                "Run:  gcloud auth application-default login\n"
                "(On Cloud Run the attached service account supplies these.)",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(2)

        log.info(
            "inference_provider",
            provider="gemini-vertex",
            project=project,
            location=settings.vertex_region,
            model=settings.gemini_classify_model,
        )
        return

    if settings.use_vertex:
        project = settings.resolved_vertex_project
        if not project:
            typer.secho(
                "Vertex is enabled but no project is set.\n"
                "Set INCIDENT_VERTEX_PROJECT or INCIDENT_GCP_PROJECT.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(2)

        adc = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
        if not adc.exists() and not os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
            typer.secho(
                "Vertex needs Application Default Credentials, which are not set up.\n"
                "Run:  gcloud auth application-default login\n"
                "(On Cloud Run the attached service account supplies these "
                "automatically.)",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(2)

        log.info(
            "inference_provider",
            provider="vertex",
            project=project,
            region=settings.vertex_region,
        )
        return

    if shutil.which("claude") is None:
        typer.secho(
            "The `claude` CLI is not on PATH. The Agent SDK needs it.\n"
            "Install it from https://claude.com/claude-code, then run `claude` once "
            "to log in — or set ANTHROPIC_API_KEY.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2)
    log.info("inference_provider", provider="anthropic-first-party")


def _wrap_for_terminal(text: str, *, limit: int, width: int = 86) -> list[str]:
    """Truncate to ``limit`` characters, then wrap. The full text is in the PR."""
    import textwrap

    body = " ".join(text.split())
    if len(body) > limit:
        body = body[:limit].rsplit(" ", 1)[0] + " … [full text in the PR body]"
    return textwrap.wrap(body, width=width) or [""]


def _report(record: RunRecord) -> None:
    """Human-readable summary on stdout; structured logs go to stderr."""
    out = typer.echo
    out("")
    out("=" * 72)
    out(f"  Run {record.run_id} — {record.outcome.upper()}")
    out("=" * 72)
    out(f"  Batch:       {record.batch_id}")
    out(f"  Fingerprint: {record.fingerprint}")

    if record.verdict:
        v = record.verdict
        out(f"  Class:       {v.failure_class}  (fix kind: {v.fix_kind})")
        out(f"  Confidence:  {v.confidence:.2f}")
        out(f"  Root cause:  {v.root_cause}")
        if v.evidence:
            out("  Evidence:")
            for e in v.evidence:
                out(f"    line {e.line_number}: {e.excerpt[:100]}")
        if v.suggested_approach:
            out(f"  Approach:    {v.suggested_approach}")

    if record.root_cause:
        r = record.root_cause
        out("")
        out(f"  ROOT CAUSE   located={r.located}  confidence={r.confidence:.2f}")
        if r.revised_class:
            out(f"    ! revised classification -> {r.revised_class}")
        for loc in r.locations:
            line = f":{loc.line_number}" if loc.line_number else ""
            out(f"    {loc.file_path}{line}")
            if loc.excerpt:
                out(f"      {loc.excerpt[:90]}")
        for line in _wrap_for_terminal(r.explanation, limit=700):
            out(f"    {line}")
        if r.proposed_fix:
            for i, line in enumerate(_wrap_for_terminal(r.proposed_fix, limit=400)):
                out(f"    {'Fix: ' if i == 0 else '     '}{line}")
        if r.blockers:
            out(f"    Blockers: {r.blockers}")

    if record.verify:
        out("")
        out(f"  Verified:    tier {record.verify.tier} — {record.verify.tier_description}")

    if reason := record.extra.get("gate_reason"):
        out(f"  Stopped:     {reason}")

    if summary := record.extra.get("agent_summary"):
        out("")
        out("  FIX AGENT SAID")
        for line in _wrap_for_terminal(summary, limit=1200):
            out(f"    {line}")

    if record.stages:
        out("")
        out(f"  {'STAGE':<12} {'TIME':>7} {'STEPS':>6} {'IN':>9} {'OUT':>7}  {'COST':>9}")
        for s in record.stages:
            cost = f"${s.cost_usd:.4f}" if s.cost_usd is not None else "—"
            steps = str(s.steps) if s.steps else "—"
            tin = f"{s.input_tokens:,}" if s.input_tokens else "—"
            tout = f"{s.output_tokens:,}" if s.output_tokens else "—"
            out(
                f"  {s.name:<12} {s.duration_s:>6.1f}s {steps:>6} "
                f"{tin:>9} {tout:>7}  {cost:>9}  {s.outcome}"
            )

        tin = sum(s.input_tokens or 0 for s in record.stages)
        tout = sum(s.output_tokens or 0 for s in record.stages)
        # Only claim a total when every stage reported one — Gemini on Vertex
        # returns tokens but no price, and printing $0.0000 would read as free.
        priced = [s for s in record.stages if s.cost_usd is not None]
        total = (
            f"${record.total_cost_usd:.4f}"
            if len(priced) == len(record.stages)
            else "not reported by provider"
        )
        out(f"  {'TOTAL':<12} {'':>7} {'':>6} {tin:>9,} {tout:>7,}  {total}")

    if record.pr_url:
        out(f"\n  PR:          {record.pr_url}")
    if record.error:
        out(f"\n  Error:       {record.error}")
    out("=" * 72)


@app.command()
def run(
    log_file: Annotated[
        Path | None, typer.Option("--log-file", help="Captured driver log, or '-' for stdin.")
    ] = None,
    repo: Annotated[
        Path | None, typer.Option("--repo", help="Local checkout of the Spark job's repo.")
    ] = None,
    batch_id: Annotated[str | None, typer.Option(help="Override the batch id.")] = None,
    job_name: Annotated[str | None, typer.Option(help="Override the job name.")] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the diff and PR body; push nothing.")
    ] = False,
    model: Annotated[
        str | None, typer.Option(help="Override the fix-stage model for the active provider.")
    ] = None,
    effort: Annotated[
        str | None, typer.Option(help="Claude only: low|medium|high|xhigh|max for the fix stage.")
    ] = None,
    max_iterations: Annotated[int | None, typer.Option(help="Fix/verify attempts.")] = None,
    stop_after: Annotated[
        str | None,
        typer.Option(
            "--stop-after",
            help="Halt after a stage: 'classify' or 'rootcause'. Useful for inspecting "
            "the pipeline without spending on a fix.",
        ),
    ] = None,
    provider: Annotated[
        str | None,
        typer.Option("--provider", help="'gemini' (Vertex AI, default) or 'claude' (Agent SDK)."),
    ] = None,
    json_logs: Annotated[bool, typer.Option("--json-logs")] = False,
) -> None:
    """Run one incident through all four agents."""
    _configure_logging(json_logs)

    overrides: dict = {}
    if dry_run:
        overrides["dry_run"] = True
    if provider:
        if provider not in {"gemini", "claude"}:
            typer.secho(
                f"--provider must be 'gemini' or 'claude', got {provider!r}",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(2)
        overrides["provider"] = provider
    active_provider = overrides.get("provider") or get_settings().provider
    if model:
        overrides["gemini_fix_model" if active_provider == "gemini" else "fix_model"] = model
    if effort:
        if active_provider == "gemini":
            typer.secho(
                "--effort is a Claude setting; Gemini has no effort tier. Ignoring it.",
                fg=typer.colors.YELLOW,
                err=True,
            )
        else:
            overrides["fix_effort"] = effort
    if max_iterations:
        overrides["max_fix_iterations"] = max_iterations
    settings = get_settings(**overrides) if overrides else get_settings()

    _check_auth(settings)

    if log_file is None:
        typer.secho(
            "--log-file is required. To consume live failures use `incident-agent serve`.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2)

    if stop_after and stop_after not in {"classify", "rootcause"}:
        typer.secho(
            f"--stop-after must be 'classify' or 'rootcause', got {stop_after!r}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2)

    from .pipeline import Outcome, run_incident

    source = FileLogSource(
        log_file, batch_id=batch_id, job_name=job_name, max_chars=settings.max_log_chars
    )
    record = asyncio.run(
        run_incident(source, settings, local_repo=repo, stop_after=stop_after)
    )
    _report(record)

    raise typer.Exit(1 if record.outcome == Outcome.ERROR else 0)


@app.command("triage-only")
def triage_only(
    log_file: Annotated[Path, typer.Argument(help="Captured driver log, or '-' for stdin.")],
    batch_id: Annotated[str | None, typer.Option(help="Override the batch id.")] = None,
    job_name: Annotated[str | None, typer.Option(help="Override the job name.")] = None,
    json_logs: Annotated[bool, typer.Option("--json-logs")] = False,
) -> None:
    """Classify a log without touching any repository."""
    _configure_logging(json_logs)
    settings = get_settings()
    _check_auth(settings)

    from .pipeline import run_incident

    source = FileLogSource(
        log_file, batch_id=batch_id, job_name=job_name, max_chars=settings.max_log_chars
    )
    record = asyncio.run(run_incident(source, settings, stop_after="classify"))
    _report(record)


@app.command()
def serve(
    subscription: Annotated[
        str, typer.Option(help="Pub/Sub subscription ID (not the full path).")
    ] = "spark-failures-sub",
    project: Annotated[str | None, typer.Option(help="GCP project. Defaults to config.")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Never push or open PRs.")] = False,
    json_logs: Annotated[bool, typer.Option("--json-logs")] = True,
) -> None:
    """Pull failures off Pub/Sub and run the agents. This is the Cloud Run Job."""
    _configure_logging(json_logs)
    settings = get_settings(dry_run=True) if dry_run else get_settings()
    _check_auth(settings)

    try:
        from .ingest.puller import PubSubPuller
    except ImportError as exc:
        typer.secho(
            f"GCP extras are not installed ({exc}). Run: uv sync --extra gcp",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2) from exc

    try:
        puller = PubSubPuller(settings, subscription=subscription, project=project)
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc

    asyncio.run(puller.run_forever())


@app.command("log-filter")
def log_filter(
    job_id: Annotated[str, typer.Argument(help="Batch or job ID to build a filter for.")],
    region: Annotated[str, typer.Option()] = "us-central1",
) -> None:
    """Print the Cloud Logging queries this agent will run.

    Paste these into Logs Explorer against a real failed job and confirm they
    return rows. A filter that matches nothing is indistinguishable from a job
    that produced no logs, so this is the first thing to verify on GCP.
    """
    from .sources.dataproc import describe_filters

    typer.echo(describe_filters(job_id, region))


@app.command()
def taxonomy() -> None:
    """Show the failure classes and which ones can produce a PR."""
    from .models import FailureClass

    typer.echo(f"{'class':<20} {'auto-PR':<9} ")
    typer.echo("-" * 32)
    for fc in FailureClass:
        mark = "yes" if fc in AUTO_PR_CLASSES else "notify only"
        typer.echo(f"{fc:<20} {mark:<9}")


@app.command()
def config() -> None:
    """Show effective settings (secrets masked)."""
    settings = get_settings()
    data = settings.model_dump()
    for key in ("anthropic_api_key", "github_token"):
        if data.get(key):
            data[key] = f"{'*' * 8}{str(data[key])[-4:]}"
    for k, v in sorted(data.items()):
        typer.echo(f"{k:<24} {v}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
