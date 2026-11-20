"""Inference-provider selection.

The Agent SDK's bundled CLI picks its transport from environment variables, so
these are the settings that decide whether a run bills to the GCP project or to
the first-party Anthropic account. Getting them silently wrong sends traffic to
the wrong place, which is why they are asserted rather than assumed.
"""

from __future__ import annotations

import pytest

from incident_agent.config import Settings


def test_vertex_env_is_complete() -> None:
    env = Settings(use_vertex=True, gcp_project="example-gcp-project").provider_env()

    assert env["CLAUDE_CODE_USE_VERTEX"] == "1"
    assert env["ANTHROPIC_VERTEX_PROJECT_ID"] == "example-gcp-project"
    assert env["CLOUD_ML_REGION"] == "global"


def test_vertex_blanks_the_api_key() -> None:
    """A stray ANTHROPIC_API_KEY outranks Vertex and silently bills first-party."""
    env = Settings(
        use_vertex=True, gcp_project="p", anthropic_api_key="sk-ant-leftover"
    ).provider_env()

    assert env["ANTHROPIC_API_KEY"] == ""


def test_vertex_project_overrides_gcp_project() -> None:
    env = Settings(use_vertex=True, gcp_project="p1", vertex_project="p2").provider_env()
    assert env["ANTHROPIC_VERTEX_PROJECT_ID"] == "p2"


def test_vertex_region_is_configurable() -> None:
    env = Settings(use_vertex=True, gcp_project="p", vertex_region="us-east5").provider_env()
    assert env["CLOUD_ML_REGION"] == "us-east5"


def test_vertex_without_a_project_fails_loudly() -> None:
    """Better a clear error at startup than an opaque failure mid-run."""
    settings = Settings(use_vertex=True, gcp_project=None, vertex_project=None)
    with pytest.raises(ValueError, match="no project is set"):
        settings.provider_env()


def test_first_party_sets_no_vertex_vars() -> None:
    env = Settings(use_vertex=False, gcp_project="p").provider_env()

    assert "CLAUDE_CODE_USE_VERTEX" not in env
    assert "ANTHROPIC_VERTEX_PROJECT_ID" not in env
    # And must not blank a key the first-party path needs.
    assert "ANTHROPIC_API_KEY" not in env


def test_git_prompt_is_disabled_on_both_paths() -> None:
    """A git credential prompt inside an agent session would hang the run."""
    for settings in (
        Settings(use_vertex=True, gcp_project="p"),
        Settings(use_vertex=False, gcp_project="p"),
    ):
        assert settings.provider_env()["GIT_TERMINAL_PROMPT"] == "0"


def test_vertex_is_off_by_default() -> None:
    """No Anthropic models are enabled on the hackathon Vertex project."""
    assert Settings().use_vertex is False


# --- Gemini provider --------------------------------------------------------


def test_gemini_is_the_default_provider() -> None:
    """Claude was not enabled on the hackathon's Vertex project; Gemini was."""
    s = Settings()
    assert s.provider == "gemini"
    assert s.gemini_classify_model.startswith("gemini-")
    assert s.vertex_region == "global"


def test_gemini_needs_a_project() -> None:
    from incident_agent.providers.gemini import make_client

    with pytest.raises(ValueError, match="needs a GCP project"):
        make_client(Settings(provider="gemini", gcp_project=None, vertex_project=None))


def test_gemini_temperature_is_low_but_not_zero() -> None:
    """Zero would make a retry after failed verification repeat the same edit."""
    assert 0.0 < Settings().gemini_temperature <= 0.4
