# One image: the worker CLI, run as a Cloud Run Job (`serve`) or by hand.
# Running it locally with `docker run` exercises exactly the same code path
# as production.
FROM python:3.13-slim

# git and gh are used by the orchestrator, never by the agent itself.
# The `claude` CLI that the Agent SDK drives ships inside the SDK wheel.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates curl gnupg \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

WORKDIR /app

# Dependency layer first so source edits do not invalidate it.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project --extra gcp

COPY src/ ./src/
COPY prompts/ ./prompts/
COPY config/ ./config/
RUN uv sync --frozen --no-dev --extra gcp

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GIT_TERMINAL_PROMPT=0

# The agent clones into a temp dir; give it a writable, non-root home.
RUN useradd --create-home --uid 1001 agent && chown -R agent:agent /app
USER agent

# The worker CLI. `incident-agent serve` is the Pub/Sub pull consumer; there is
# no push endpoint, so no second entrypoint.
ENTRYPOINT ["incident-agent"]
CMD ["--help"]

# NOTE (P1): verification runs the *target* repo's test suite, which needs that
# repo's own dependencies and — for PySpark local mode — a JRE. This base image
# has neither. Before enabling tier-2 verification in Cloud Run, either add a
# JRE and a per-repo `uv sync` step to the worker, or pin verification to
# tier 1. Shipping without this would silently downgrade every PR to
# "weakly verified".
