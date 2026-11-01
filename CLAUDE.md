# Working in this repo

Spark incident agent: failed Dataproc log → classify → locate → patch → verify →
draft PR. Read `README.md` first; it is current and is the design document.

## Commands

```bash
uv sync                         # Python 3.13, deps incl. pyspark for local verification
uv run pytest                   # unit tests, no network, ~5s
uv run pytest -m golden         # live classifier regression — costs money, needs ADC
uv run ruff check .             # lint (I, UP, B, SIM enabled)
uv run incident-agent run --log-file tests/fixtures/logs/schema_mismatch.log \
    --repo examples/broken-spark-job --dry-run     # full pipeline, pushes nothing
```

## Invariants — do not weaken

- **The agent never holds a credential.** The GitHub token travels only as a
  per-command `git -c http.extraheader` (`repo.auth_args`); it is never in a URL
  or in `.git/config`. Subprocesses that run repo code (agent shell, verify)
  use `guardrails.subprocess_env`, an allow-list with no secrets.
- **Every tool call is checked in code, not prompt.** Gemini: `tools.RepoTools.dispatch`.
  Claude: `guardrails.make_pretooluse_hook`. Reads, writes and bash all go through
  `guardrails.check_*`. If you add a tool, route it through those.
- **Gates are enforced in `models.py`**, not in prompts: no evidence → `unknown`;
  not located → no fix agent; notify-only class → no repo access.
- **Provider errors are infrastructure errors.** `pipeline._check_provider` turns
  `provider_error` into `Outcome.ERROR` so the Pub/Sub consumer nacks. Never
  convert one into a verdict.
- **Verification must not overclaim.** Tier 0 means nothing ran. Pre-existing
  test failures (baseline) hold the result at tier 1 and are named in the PR.

## Layout

`src/incident_agent/pipeline.py` orchestrates; `agents/` are the four stages;
`providers/gemini.py` + `tools.py` is the Gemini path, `agents/base.py` +
`guardrails.py` hook is the Claude path. `repo.py`/`pr.py` own git and GitHub.
`notify.py` and `store.py` are where finished runs go. Prompts live in
`prompts/`; the taxonomy in `prompts/taxonomy.md` must match `models.FailureClass`.

## Tests

Add a test for every guardrail change — `tests/test_guardrails.py` asserts on the
denial *reason*. Push-path tests in `tests/test_pr_push.py` run against a real
local bare repo with `gh` faked. New captured failures go in
`tests/fixtures/logs/` and into `GOLDEN` in `tests/test_triage_golden.py`.
