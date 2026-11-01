"""Core data models passed between pipeline stages."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


def unescape_prose(value: str) -> str:
    """Undo over-escaping in model-generated prose.

    Structured output occasionally emits a literal backslash-n or backslash-quote
    *inside* the JSON string, so the parsed value contains the two characters
    rather than the character they denote. Left alone this renders as `\\n\\n`
    in the pull request body.
    """
    if not value or "\\" not in value:
        return value
    return (
        value.replace("\\r\\n", "\n")
        .replace("\\n", "\n")
        .replace("\\t", "\t")
        .replace('\\"', '"')
        .replace("\\'", "'")
    )


class FixKind(StrEnum):
    """What kind of change, if any, could resolve the failure."""

    CODE = "code"
    CONFIG = "config"
    DATA = "data"
    INFRA = "infra"
    NONE = "none"


class FailureClass(StrEnum):
    """The failure taxonomy. Keep in sync with prompts/taxonomy.md."""

    SCHEMA_MISMATCH = "schema_mismatch"
    TYPE_COERCION = "type_coercion"
    NULL_HANDLING = "null_handling"
    BAD_JOIN = "bad_join"
    EXECUTOR_OOM = "executor_oom"
    SHUFFLE_SKEW = "shuffle_skew"
    DRIVER_OOM = "driver_oom"
    MISSING_INPUT = "missing_input"
    PERMISSION = "permission"
    TRANSIENT = "transient"
    UNKNOWN = "unknown"


#: Classes the agent is allowed to open a PR for. Everything else is notify-only.
AUTO_PR_CLASSES: frozenset[FailureClass] = frozenset(
    {
        FailureClass.SCHEMA_MISMATCH,
        FailureClass.TYPE_COERCION,
        FailureClass.NULL_HANDLING,
        FailureClass.BAD_JOIN,
        FailureClass.EXECUTOR_OOM,
        FailureClass.SHUFFLE_SKEW,
        FailureClass.DRIVER_OOM,
    }
)


class FailureEvent(BaseModel):
    """A Spark job failure, normalised across log sources."""

    batch_id: str
    job_name: str = "unknown"
    project: str | None = None
    region: str | None = None
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    labels: dict[str, str] = Field(default_factory=dict)

    driver_log: str = ""
    container_log: str = ""
    spark_conf: dict[str, str] = Field(default_factory=dict)
    log_url: str | None = None

    @property
    def combined_log(self) -> str:
        parts = []
        if self.driver_log:
            parts.append("===== DRIVER LOG =====\n" + self.driver_log)
        if self.container_log:
            parts.append("===== YARN CONTAINER LOG =====\n" + self.container_log)
        return "\n\n".join(parts)

    def fingerprint(self) -> str:
        """Stable ID for a *recurring* failure, used to dedupe PRs.

        Deliberately excludes batch_id and timestamps: the same bug failing
        nightly must map to one fingerprint, not thirty.
        """
        from .redact import normalise_for_fingerprint

        basis = f"{self.job_name}\n{normalise_for_fingerprint(self.combined_log)}"
        return hashlib.sha256(basis.encode()).hexdigest()[:16]


class EvidenceRef(BaseModel):
    """A cited line from the log. Triage without evidence is not trusted."""

    line_number: int = Field(description="1-based line number in the provided log")
    excerpt: str = Field(description="The verbatim log line")


class TriageVerdict(BaseModel):
    """Structured classification of a failure."""

    failure_class: FailureClass
    fix_kind: FixKind
    confidence: float = Field(ge=0.0, le=1.0)
    root_cause: str
    evidence: list[EvidenceRef] = Field(default_factory=list)
    suggested_approach: str = ""

    @field_validator("root_cause", "suggested_approach", mode="after")
    @classmethod
    def _clean_prose(cls, v: str) -> str:
        return unescape_prose(v)

    def is_actionable(self, threshold: float) -> tuple[bool, str]:
        """Whether this verdict should proceed to the fix stage."""
        if not self.evidence:
            return False, "no evidence cited from the log"
        if self.failure_class not in AUTO_PR_CLASSES:
            return False, f"class '{self.failure_class}' is notify-only"
        if self.confidence < threshold:
            return False, f"confidence {self.confidence:.2f} below threshold {threshold:.2f}"
        return True, ""


class CodeLocation(BaseModel):
    """A specific place in the repository the root-cause agent identified."""

    file_path: str = Field(description="Repository-relative path")
    line_number: int | None = Field(default=None, description="1-based line, if pinpointed")
    excerpt: str = Field(default="", description="The offending source line(s)")


class RootCauseReport(BaseModel):
    """Output of the root-cause agent, which has read-only repository access.

    This is the stage that turns 'the log says X' into 'line N of file F does
    X'. If it cannot locate the cause in the repository, the fix agent is never
    started — patching a file nobody identified is how wrong PRs get opened.
    """

    located: bool = Field(description="Whether the cause was found in the repository")
    confirms_classification: bool = Field(
        description="False if the code contradicts the classifier's verdict"
    )
    revised_class: FailureClass | None = Field(
        default=None, description="Set when the code shows the classifier was wrong"
    )
    locations: list[CodeLocation] = Field(default_factory=list)
    explanation: str = Field(description="Why this code produces that log")
    proposed_fix: str = Field(default="", description="What a fix would need to change")
    confidence: float = Field(ge=0.0, le=1.0)
    blockers: str = Field(
        default="", description="Why no safe fix exists, when located is false"
    )

    @field_validator("explanation", "proposed_fix", "blockers", mode="after")
    @classmethod
    def _clean_prose(cls, v: str) -> str:
        return unescape_prose(v)

    def is_actionable(self, threshold: float) -> tuple[bool, str]:
        if not self.located:
            return False, f"root cause not located in the repository: {self.blockers or 'no detail'}"
        if not self.locations:
            return False, "no code location identified"
        if self.confidence < threshold:
            return False, f"root-cause confidence {self.confidence:.2f} below {threshold:.2f}"
        return True, ""


class VerifyResult(BaseModel):
    """Outcome of the tiered verification step."""

    tier: int = 0
    passed: bool = False
    output: str = ""
    commands_run: list[str] = Field(default_factory=list)

    @property
    def tier_description(self) -> str:
        return {
            0: "NOTHING WAS VERIFIED — no check ran against this change",
            1: "static checks only (weakly verified)",
            2: "repo test suite passed",
            3: "re-submitted to Dataproc",
        }.get(self.tier, "unknown")


class FixResult(BaseModel):
    """What the agent changed."""

    changed: bool = False
    diff: str = ""
    files_changed: list[str] = Field(default_factory=list)
    rationale: str = ""
    iterations: int = 0
    cost_usd: float | None = None
    stopped_reason: str = ""
    #: The model that produced the patch, for honest commit attribution.
    model: str = ""


class PRNarrative(BaseModel):
    """Human-facing PR copy, synthesised from every prior stage.

    Only the prose is model-generated. Branch names, commits and the push are
    deterministic — an agent never holds the push credential.
    """

    title: str = Field(description="PR title, imperative mood, under 70 characters")
    summary: str = Field(description="What broke and what changed, 2-4 sentences")
    reviewer_focus: str = Field(
        description="The specific thing a reviewer should check before merging"
    )
    residual_risk: str = Field(
        default="", description="What remains unverified, or empty if nothing"
    )

    @field_validator("title", "summary", "reviewer_focus", "residual_risk", mode="after")
    @classmethod
    def _clean_prose(cls, v: str) -> str:
        return unescape_prose(v)


class StageRecord(BaseModel):
    """Per-agent accounting, so cost and latency are attributable to a stage."""

    name: str
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    duration_s: float = 0.0
    #: Populated by providers that report a price. Gemini on Vertex reports
    #: tokens but not cost, so this stays None there rather than being guessed.
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    model: str = ""
    outcome: str = ""
    #: Tool-calling turns used, for loop-based stages.
    steps: int = 0


class RunRecord(BaseModel):
    """One end-to-end incident run, persisted for audit and dedupe."""

    run_id: str
    batch_id: str
    fingerprint: str
    job_name: str = ""
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    outcome: str = "started"
    verdict: TriageVerdict | None = None
    root_cause: RootCauseReport | None = None
    verify: VerifyResult | None = None
    pr_url: str | None = None
    error: str | None = None
    stages: list[StageRecord] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)

    @property
    def total_cost_usd(self) -> float:
        return sum(s.cost_usd or 0.0 for s in self.stages)
