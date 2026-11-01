"""The Gemini provider's failure behaviour, with the network faked.

Retries and the token ceiling are what stand between a Vertex hiccup and a
lost incident or an unbounded bill; neither had coverage.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from incident_agent.config import Settings
from incident_agent.providers import gemini
from incident_agent.tools import RepoTools


class Out(BaseModel):
    ok: bool


class Transient(Exception):
    code = 503


class Permanent(Exception):
    code = 400


def _usage(prompt: int = 10, out: int = 2):
    return SimpleNamespace(prompt_token_count=prompt, candidates_token_count=out)


class FakeModels:
    """Stands in for ``client.models``; fails ``failures`` times, then answers."""

    def __init__(self, failures: int = 0, error: type[Exception] = Transient, response=None):
        self.failures = failures
        self.error = error
        self.calls = 0
        self.response = response or SimpleNamespace(parsed=Out(ok=True), usage_metadata=_usage(), text="")

    def generate_content(self, **kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error(f"{self.error.__name__} on call {self.calls}")
        return self.response


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        provider="gemini",
        gcp_project="p",
        gemini_max_retries=3,
        gemini_retry_backoff_s=0.0,
        gemini_max_stage_tokens=25,
    )


def _patch_client(monkeypatch, models: FakeModels) -> None:
    monkeypatch.setattr(gemini, "make_client", lambda _s: SimpleNamespace(models=models))


async def _call(settings: Settings) -> tuple[Out | None, object]:
    return await gemini.structured_call(
        stage="classify",
        system_prompt="s",
        user_prompt="u",
        output_model=Out,
        settings=settings,
        model="gemini-test",
    )


async def test_transient_errors_are_retried(monkeypatch, settings) -> None:
    models = FakeModels(failures=2)
    _patch_client(monkeypatch, models)

    out, record = await _call(settings)

    assert out == Out(ok=True)
    assert models.calls == 3
    assert record.outcome == "success"


async def test_retries_are_bounded_and_then_reported_as_provider_error(monkeypatch, settings) -> None:
    models = FakeModels(failures=99)
    _patch_client(monkeypatch, models)

    out, record = await _call(settings)

    assert out is None
    assert record.outcome == "provider_error"
    assert models.calls == settings.gemini_max_retries + 1


async def test_permanent_errors_fail_immediately(monkeypatch, settings) -> None:
    models = FakeModels(failures=99, error=Permanent)
    _patch_client(monkeypatch, models)

    out, record = await _call(settings)

    assert out is None and record.outcome == "provider_error"
    assert models.calls == 1


async def test_agent_loop_stops_at_the_token_ceiling(monkeypatch, settings, tmp_path: Path) -> None:
    """Each step re-sends the whole history, so cumulative input tokens are the
    spend. With no price from Vertex, this ceiling is the only budget there is."""
    (tmp_path / "jobs").mkdir()
    (tmp_path / "jobs" / "x.py").write_text("x = 1\n")

    # A model that never stops calling tools.
    call = SimpleNamespace(name="glob", args={"pattern": "**/*.py"})
    content = SimpleNamespace(role="model", parts=[SimpleNamespace(function_call=call, text=None)])
    response = SimpleNamespace(candidates=[SimpleNamespace(content=content)], usage_metadata=_usage(10), text="")
    models = FakeModels(response=response)
    _patch_client(monkeypatch, models)

    tools = RepoTools(worktree=tmp_path, read_only=True)
    _, outcome = await gemini.agent_loop(
        stage="rootcause",
        system_prompt="s",
        user_prompt="u",
        tools=tools,
        settings=settings,
        model="gemini-test",
        max_steps=40,
    )

    assert outcome.hit_token_budget
    assert outcome.record.outcome == "token_budget"
    # 10 tokens per step, ceiling 25: the third step crosses it.
    assert outcome.steps == 3 and models.calls == 3
    assert not outcome.hit_step_limit
