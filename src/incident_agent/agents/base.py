"""Shared plumbing for the four agents.

Every stage runs through one of two helpers:

* :func:`structured_call` — no tools, returns a validated Pydantic model.
  Used by the classifier.
* :func:`agent_session` — tools enabled, returns the transcript plus accounting.
  Used by the root-cause and fix agents.

Both funnel through the Agent SDK so the whole tool has a single auth path.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    ResultMessage,
    TextBlock,
    query,
)
from pydantic import BaseModel, ValidationError

from ..models import StageRecord
from ..schema import schema_for

log = structlog.get_logger(__name__)

@dataclass(slots=True)
class StageOutcome:
    """What a stage produced, plus what it cost."""

    text: str = ""
    structured: dict[str, Any] | None = None
    record: StageRecord = field(default_factory=lambda: StageRecord(name=""))
    violations: list[str] = field(default_factory=list)


async def structured_call[T: BaseModel](
    *,
    stage: str,
    system_prompt: str,
    user_prompt: str,
    output_model: type[T],
    model: str,
    effort: str,
    max_turns: int = 4,
    env: dict[str, str] | None = None,
) -> tuple[T | None, StageRecord]:
    """Run a tool-less agent and validate its structured output."""
    started = time.monotonic()
    record = StageRecord(name=stage, model=model)

    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=model,
        effort=effort,
        tools=[],
        setting_sources=[],
        output_format={"type": "json_schema", "schema": schema_for(output_model)},
        # Structured output is delivered through an internal tool call, which
        # consumes a turn — 1 is never enough.
        max_turns=max_turns,
        env=env or {},
    )

    raw: dict | None = None
    async for message in query(prompt=user_prompt, options=options):
        if isinstance(message, ResultMessage):
            raw = getattr(message, "structured_output", None)
            record.cost_usd = message.total_cost_usd
            record.outcome = message.subtype or ""
            usage = message.usage or {}
            log.info(
                f"{stage}_usage",
                model=model,
                cost_usd=message.total_cost_usd,
                cache_read=usage.get("cache_read_input_tokens", 0),
                cache_write=usage.get("cache_creation_input_tokens", 0),
            )

    record.duration_s = round(time.monotonic() - started, 2)

    if not raw:
        record.outcome = "no_structured_output"
        return None, record

    try:
        return output_model.model_validate(raw), record
    except ValidationError as exc:
        log.warning(f"{stage}_invalid_output", error=str(exc)[:400])
        record.outcome = "validation_failed"
        return None, record


async def agent_session[T: BaseModel](
    *,
    stage: str,
    system_prompt: str,
    user_prompt: str,
    worktree: Path,
    model: str,
    effort: str,
    allowed_tools: list[str],
    max_turns: int,
    max_budget_usd: float,
    hook: Any | None = None,
    violations: list[str] | None = None,
    output_model: type[T] | None = None,
    env: dict[str, str] | None = None,
) -> tuple[T | None, StageOutcome]:
    """Run an agent with repository tools, optionally with structured output."""
    started = time.monotonic()
    record = StageRecord(name=stage, model=model)
    sink = violations if violations is not None else []

    kwargs: dict[str, Any] = {}
    if output_model is not None:
        kwargs["output_format"] = {"type": "json_schema", "schema": schema_for(output_model)}
    if hook is not None:
        kwargs["hooks"] = {"PreToolUse": [HookMatcher(matcher=None, hooks=[hook])]}

    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=model,
        effort=effort,
        thinking={"type": "adaptive"},
        cwd=str(worktree),
        permission_mode="acceptEdits",
        allowed_tools=allowed_tools,
        disallowed_tools=["WebFetch", "WebSearch", "Task"],
        # Without this the agent inherits the host's ~/.claude settings, so a
        # laptop and Cloud Run would behave differently.
        setting_sources=[],
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
        env=env or {"GIT_TERMINAL_PROMPT": "0"},
        **kwargs,
    )

    texts: list[str] = []
    raw: dict | None = None

    async for message in query(prompt=user_prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    texts.append(block.text)
        elif isinstance(message, ResultMessage):
            raw = getattr(message, "structured_output", None)
            record.cost_usd = message.total_cost_usd
            record.outcome = message.subtype or ""
            log.info(
                f"{stage}_session_complete",
                subtype=message.subtype,
                cost_usd=message.total_cost_usd,
                turns=getattr(message, "num_turns", None),
            )

    record.duration_s = round(time.monotonic() - started, 2)
    outcome = StageOutcome(
        text="\n\n".join(t.strip() for t in texts if t.strip()).strip(),
        structured=raw,
        record=record,
        violations=sink,
    )

    if output_model is None or not raw:
        return None, outcome

    try:
        return output_model.model_validate(raw), outcome
    except ValidationError as exc:
        log.warning(f"{stage}_invalid_output", error=str(exc)[:400])
        record.outcome = "validation_failed"
        return None, outcome
