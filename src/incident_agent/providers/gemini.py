"""Gemini on Vertex AI.

Two entry points mirroring the Claude provider:

* :func:`structured_call` — no tools, returns a validated Pydantic instance.
  ``google-genai`` accepts the model class directly as ``response_schema`` and
  returns a parsed instance, so the models in :mod:`incident_agent.models` are
  reused unchanged.
* :func:`agent_loop` — the function-calling loop. Gemini has no built-in file
  tools, so :class:`~incident_agent.tools.RepoTools` supplies them and enforces
  the guardrails inside the dispatcher.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import structlog
from pydantic import BaseModel

from ..config import Settings
from ..models import StageRecord
from ..tools import RepoTools

log = structlog.get_logger(__name__)

#: Hard stop on the loop, independent of the model's own stopping behaviour.
DEFAULT_MAX_STEPS = 40

#: HTTP statuses worth retrying. Anything else is a real error and surfaces
#: immediately — retrying a 400 would only burn the budget.
_RETRYABLE_CODES = {408, 429, 500, 502, 503, 504}
_RETRYABLE_NAMES = {
    "ResourceExhausted",
    "ServiceUnavailable",
    "DeadlineExceeded",
    "InternalServerError",
    "TooManyRequests",
    "Aborted",
}


def _is_retryable(exc: BaseException) -> bool:
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if isinstance(code, int) and code in _RETRYABLE_CODES:
        return True
    return type(exc).__name__ in _RETRYABLE_NAMES


async def _generate(client: Any, settings: Settings, stage: str, **kwargs: Any) -> Any:
    """One ``generate_content`` call with bounded retry on transient errors.

    A 429 or 503 from Vertex must not become a diagnosis. Without this the
    exception was swallowed into ``provider_error``, the classifier returned
    ``unknown``, and the Pub/Sub message was acked — the incident was lost.
    """
    attempts = max(1, settings.gemini_max_retries + 1)
    for attempt in range(1, attempts + 1):
        try:
            return await asyncio.to_thread(client.models.generate_content, **kwargs)
        except Exception as exc:  # noqa: BLE001 - classified below
            if attempt >= attempts or not _is_retryable(exc):
                raise
            delay = settings.gemini_retry_backoff_s * (2 ** (attempt - 1))
            log.warning(
                f"{stage}_retrying",
                attempt=attempt,
                of=attempts,
                delay_s=delay,
                error=f"{type(exc).__name__}: {str(exc)[:200]}",
            )
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


def make_client(settings: Settings):
    """Vertex-backed genai client.

    Configured explicitly rather than through process-wide environment
    variables so a single process can talk to more than one project, and so
    the settings object stays the single source of truth.
    """
    from google import genai
    from google.genai.types import HttpOptions

    project = settings.resolved_vertex_project
    if not project:
        raise ValueError(
            "Gemini needs a GCP project — set INCIDENT_VERTEX_PROJECT or INCIDENT_GCP_PROJECT"
        )

    return genai.Client(
        vertexai=True,
        project=project,
        location=settings.vertex_region,
        http_options=HttpOptions(api_version="v1"),
    )


def _record_usage(response: Any, record: StageRecord) -> None:
    """Token accounting. Cost is left unset — see the note below."""
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return
    record.input_tokens = (record.input_tokens or 0) + (usage.prompt_token_count or 0)
    record.output_tokens = (record.output_tokens or 0) + (usage.candidates_token_count or 0)


async def structured_call[T: BaseModel](
    *,
    stage: str,
    system_prompt: str,
    user_prompt: str,
    output_model: type[T],
    settings: Settings,
    model: str,
) -> tuple[T | None, StageRecord]:
    """Single structured-output call. Returns a validated instance, or None.

    A ``None`` with ``record.outcome == "provider_error"`` means the provider
    failed after retries; the pipeline treats that as an infrastructure error,
    not a verdict.
    """
    from google.genai.types import GenerateContentConfig

    started = time.monotonic()
    record = StageRecord(name=stage, model=model)
    client = make_client(settings)

    try:
        response = await _generate(
            client,
            settings,
            stage,
            model=model,
            contents=user_prompt,
            config=GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="application/json",
                response_schema=output_model,
                temperature=settings.gemini_temperature,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - a provider error must not kill the run
        log.warning(f"{stage}_call_failed", error=str(exc)[:400])
        record.outcome = "provider_error"
        record.duration_s = round(time.monotonic() - started, 2)
        return None, record

    _record_usage(response, record)
    record.duration_s = round(time.monotonic() - started, 2)

    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, output_model):
        record.outcome = "success"
        log.info(
            f"{stage}_usage",
            model=model,
            input_tokens=record.input_tokens,
            output_tokens=record.output_tokens,
        )
        return parsed, record

    log.warning(f"{stage}_unparsed", text=(getattr(response, "text", "") or "")[:300])
    record.outcome = "no_structured_output"
    return None, record


@dataclass(slots=True)
class LoopOutcome:
    """Transcript and accounting from an agentic loop."""

    text: str = ""
    record: StageRecord = field(default_factory=lambda: StageRecord(name=""))
    violations: list[str] = field(default_factory=list)
    steps: int = 0
    hit_step_limit: bool = False
    #: Cumulative input tokens for the stage crossed ``gemini_max_stage_tokens``.
    hit_token_budget: bool = False


async def agent_loop[T: BaseModel](
    *,
    stage: str,
    system_prompt: str,
    user_prompt: str,
    tools: RepoTools,
    settings: Settings,
    model: str,
    output_model: type[T] | None = None,
    max_steps: int = DEFAULT_MAX_STEPS,
) -> tuple[T | None, LoopOutcome]:
    """Run the function-calling loop until the model stops calling tools.

    Gemini accepts ``tools`` and ``response_schema`` together: it emits function
    calls while exploring, then a schema-valid JSON object on the turn it stops
    calling tools. That avoids a second summarising call, so nothing the agent
    learned is lost in a hand-off.

    Several function calls may come back in one turn; all are executed and their
    results returned together, which keeps the transcript well-formed.

    Two ceilings, because the Agent SDK's ``max_budget_usd`` does not exist here:
    ``max_steps`` bounds the turns, and ``settings.gemini_max_stage_tokens``
    bounds cumulative input tokens — each step re-sends the whole history, so
    the latter is what actually tracks spend.
    """
    from google.genai.types import (
        Content,
        FunctionResponse,
        GenerateContentConfig,
        Part,
        Tool,
    )

    started = time.monotonic()
    record = StageRecord(name=stage, model=model)
    client = make_client(settings)

    config_kwargs: dict[str, Any] = {
        "system_instruction": system_prompt,
        "tools": [Tool(function_declarations=tools.declarations())],
        "temperature": settings.gemini_temperature,
    }
    if output_model is not None:
        config_kwargs["response_mime_type"] = "application/json"
        config_kwargs["response_schema"] = output_model
    config = GenerateContentConfig(**config_kwargs)

    history: list[Content] = [Content(role="user", parts=[Part(text=user_prompt)])]
    texts: list[str] = []
    parsed: T | None = None
    steps = 0
    hit_limit = False
    hit_budget = False

    while steps < max_steps:
        steps += 1

        try:
            response = await _generate(
                client, settings, stage, model=model, contents=history, config=config
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(f"{stage}_call_failed", step=steps, error=str(exc)[:400])
            record.outcome = "provider_error"
            break

        _record_usage(response, record)
        if (record.input_tokens or 0) > settings.gemini_max_stage_tokens:
            hit_budget = True
            record.outcome = "token_budget"
            log.warning(
                f"{stage}_hit_token_budget",
                input_tokens=record.input_tokens,
                limit=settings.gemini_max_stage_tokens,
                steps=steps,
            )
            break

        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            record.outcome = "no_candidates"
            break

        content = candidates[0].content
        parts = list(getattr(content, "parts", None) or [])
        history.append(content)

        calls = [p.function_call for p in parts if getattr(p, "function_call", None)]
        for part in parts:
            if getattr(part, "text", None):
                texts.append(part.text)

        if not calls:
            if output_model is not None:
                candidate_parsed = getattr(response, "parsed", None)
                if isinstance(candidate_parsed, output_model):
                    parsed = candidate_parsed
                    record.outcome = "success"
                else:
                    record.outcome = "no_structured_output"
                    log.warning(
                        f"{stage}_unparsed",
                        text=(getattr(response, "text", "") or "")[:300],
                    )
            else:
                record.outcome = "success"
            break

        results: list[Part] = []
        for call in calls:
            args = dict(call.args or {})
            log.info(f"{stage}_tool_call", step=steps, tool=call.name)
            result = await asyncio.to_thread(tools.dispatch, call.name, args)
            results.append(
                Part(
                    function_response=FunctionResponse(
                        name=call.name, response={"result": result}
                    )
                )
            )
        history.append(Content(role="user", parts=results))
    else:
        hit_limit = True
        record.outcome = "max_steps"
        log.warning(f"{stage}_hit_step_limit", max_steps=max_steps)

    record.duration_s = round(time.monotonic() - started, 2)
    log.info(
        f"{stage}_loop_complete",
        steps=steps,
        outcome=record.outcome,
        input_tokens=record.input_tokens,
        output_tokens=record.output_tokens,
        violations=len(tools.violations),
    )

    outcome = LoopOutcome(
        text="\n\n".join(t.strip() for t in texts if t.strip()).strip(),
        record=record,
        violations=tools.violations,
        steps=steps,
        hit_step_limit=hit_limit,
        hit_token_budget=hit_budget,
    )
    return parsed, outcome
