"""Runtime configuration. Env vars in local mode, Secret Manager on GCP."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent.parent
PROMPTS_DIR = PROJECT_ROOT / "prompts"


class Settings(BaseSettings):
    """All tunables. Every field is overridable by an ``INCIDENT_``-prefixed env var."""

    model_config = SettingsConfigDict(
        env_prefix="INCIDENT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- credentials ---
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    github_token: str | None = Field(default=None, alias="GITHUB_TOKEN")

    # --- inference provider ---
    #: Route the agents through Claude on Vertex AI instead of the first-party
    #: API. Off by default because `hack-team-treasury-geeks-2026` has **no
    #: Anthropic publisher models enabled** — verified 2026-07-23, every
    #: `publishers/anthropic/models/*` returns 404 at global, us-east5 and
    #: us-central1, while `publishers/google/models/gemini-3.5-flash` returns
    #: 200. Turn this on once an admin enables Claude in Vertex Model Garden.
    use_vertex: bool = False
    #: Defaults to `gcp_project` when unset.
    vertex_project: str | None = None
    #: `global` is the recommended Vertex region for Claude; a specific region
    #: (e.g. `us-east5`) also works if the model is served there.
    vertex_region: str = "global"

    # --- inference provider selection ---
    #: `gemini` (Vertex, the hackathon project's only available family) or
    #: `claude` (Claude Agent SDK, first-party API).
    provider: str = "gemini"

    #: Gemini model per agent. `gemini-3.5-flash` and `gemini-2.5-flash` are
    #: confirmed available at location `global` on this project.
    gemini_classify_model: str = "gemini-3.5-flash"
    gemini_rootcause_model: str = "gemini-3.5-flash"
    gemini_fix_model: str = "gemini-3.5-flash"
    gemini_narrate_model: str = "gemini-3.5-flash"
    #: Low but non-zero: deterministic enough for classification, not so rigid
    #: that a retry after failed verification repeats the same edit.
    gemini_temperature: float = 0.2
    #: Hard ceiling on tool-calling turns per agentic stage.
    gemini_max_steps: int = 40
    #: Cumulative input tokens per agentic stage. Each step re-sends the whole
    #: history, so this — not the step count — is what bounds spend. Vertex
    #: reports no price, so the ceiling is in tokens.
    gemini_max_stage_tokens: int = 600_000
    #: Retries on 429/5xx before a call is declared failed. Exponential backoff
    #: from ``gemini_retry_backoff_s``.
    gemini_max_retries: int = 3
    gemini_retry_backoff_s: float = 1.0

    # --- models, per agent (claude provider) ---
    #: 1. Classifier — log only, no repo. Cheapest stage; set to
    #: `claude-haiku-4-5` if triage volume gets expensive.
    triage_model: str = "claude-opus-4-8"
    triage_effort: str = "medium"

    #: 2. Root cause — reads the repository. Needs to reason across log and
    #: code, so this is not the place to economise.
    rootcause_model: str = "claude-opus-4-8"
    rootcause_effort: str = "high"

    #: 3. Fix — writes the patch. `xhigh` is the recommended tier for coding
    #: and agentic work.
    fix_model: str = "claude-opus-4-8"
    fix_effort: str = "xhigh"

    #: 4. Narrator — PR prose. Short output, no tools.
    narrate_model: str = "claude-opus-4-8"
    narrate_effort: str = "medium"

    # --- gating ---
    #: Classifier verdicts below this never reach the repository.
    confidence_threshold: float = 0.7
    #: Root-cause reports below this never reach the fix agent.
    rootcause_threshold: float = 0.7

    # --- monorepo scoping ---
    #: Subdirectory holding the Spark job, when the target is a monorepo.
    #: Edits are confined to this subtree; empty means the whole repo.
    spark_subdir: str = "incident-resolver/scala-spark-application"
    #: Fix -> verify -> fix attempts before giving up.
    max_fix_iterations: int = 3
    #: Hard ceiling per incident, enforced by the Agent SDK itself.
    max_budget_usd: float = 5.0
    #: Agent turns per fix attempt.
    max_turns: int = 60

    # --- guardrails ---
    #: Globs the agent may edit. Anything else is denied by the PreToolUse hook.
    #: Relative to the Spark subtree when `spark_subdir` is set. Covers both
    #: PySpark and Scala/JVM layouts.
    editable_globs: list[str] = Field(
        default_factory=lambda: [
            "src/**",
            "jobs/**",
            "conf/**",
            "config/**",
            "spark/**",
            "project/**",
            "**/*.py",
            "**/*.scala",
            "**/*.java",
            "**/*.sql",
            "**/*.yaml",
            "**/*.yml",
            "**/*.conf",
            "**/*.properties",
            "build.sbt",
            "pom.xml",
        ]
    )
    #: Never editable, even if matched above.
    protected_globs: list[str] = Field(
        default_factory=lambda: [
            ".git/**",
            ".github/workflows/**",
            "**/*.tf",
            "**/Dockerfile",
            "**/.env*",
            "**/secrets/**",
            "**/credentials*",
        ]
    )

    # --- behaviour ---
    dry_run: bool = False
    draft_pr: bool = True
    pr_branch_prefix: str = "incident-agent"
    #: Truncation budget for log text entering a prompt (characters).
    max_log_chars: int = 60_000
    #: Run the target repo's tests once *before* the fix, so verification can
    #: tell a pre-existing failure from one the patch introduced.
    verify_baseline: bool = True

    # --- reporting ---
    #: Where non-PR outcomes go. Empty means the structured log only. A URL
    #: receives a Slack-compatible ``{"text": ...}`` POST per finished run.
    notify_webhook_url: str | None = None
    #: ``none`` | ``json`` | ``firestore``. Where finished RunRecords are kept.
    run_store: str = "none"
    #: Directory for the ``json`` store.
    run_store_dir: str = ".incident-agent/runs"

    # --- gcp (unused in local mode) ---
    gcp_project: str | None = "hack-team-treasury-geeks-2026"
    gcp_region: str = "us-central1"
    pubsub_topic: str = "spark-failures"
    worker_job_name: str = "agent-worker"
    firestore_collection: str = "runs"

    @property
    def resolved_vertex_project(self) -> str | None:
        return self.vertex_project or self.gcp_project

    def provider_env(self) -> dict[str, str]:
        """Environment handed to the Agent SDK's CLI subprocess.

        The bundled `claude` binary reads these to pick its transport. Setting
        them per-session rather than globally keeps the choice explicit and
        testable, and avoids leaking provider config into unrelated processes.
        """
        env = {"GIT_TERMINAL_PROMPT": "0"}
        if not self.use_vertex:
            return env

        project = self.resolved_vertex_project
        if not project:
            raise ValueError(
                "use_vertex is on but no project is set — "
                "set INCIDENT_VERTEX_PROJECT or INCIDENT_GCP_PROJECT"
            )

        env.update(
            {
                "CLAUDE_CODE_USE_VERTEX": "1",
                "ANTHROPIC_VERTEX_PROJECT_ID": project,
                "CLOUD_ML_REGION": self.vertex_region,
                # Vertex authenticates with Application Default Credentials; an
                # API key left in the environment would take precedence and
                # silently send the traffic first-party instead.
                "ANTHROPIC_API_KEY": "",
            }
        )
        return env

    def prompt(self, name: str) -> str:
        """Read a prompt file from ``prompts/``."""
        path = PROMPTS_DIR / name
        if not path.exists():
            raise FileNotFoundError(f"prompt file not found: {path}")
        return path.read_text(encoding="utf-8")


_settings: Settings | None = None


def get_settings(**overrides) -> Settings:
    """Process-wide settings singleton. ``overrides`` force a rebuild."""
    global _settings
    if overrides:
        _settings = Settings(**overrides)
    elif _settings is None:
        _settings = Settings()
    return _settings
