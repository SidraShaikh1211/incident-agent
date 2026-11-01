# Spark Incident Agent

Reads a failed Spark job's logs, works out why it broke, patches the cause in
the job's repository, runs the tests, and opens a **draft pull request** with a
root-cause writeup.

It never merges. It never runs git. A human reviews every change.

```
Dataproc batch fails
  → Cloud Logging sink → Pub/Sub → agent pulls the message
  → fetch logs → redact secrets
  →  1. CLASSIFY   taxonomy match from the log alone.   no repo access
     ─ gate: class + confidence + cited evidence ─
  →  2. ROOT CAUSE locate the code that did it.         read-only repo
     ─ gate: located + confidence ─
  →  3. FIX        patch it, run the tests.             write, scoped
  →  4. NARRATE    compose the PR prose.                no repo access
  → draft PR
```

Each stage gates the next. A notify-only failure never reaches the repository;
an unlocated root cause never reaches an agent that can write files.

## Status

**Working end to end from a log file.** Four agents, gated, producing a
verified patch and a draft PR.

**Every finished run is announced and can be stored** — see *Where a finished
run goes*.

**Pub/Sub + Dataproc ingestion is written but unverified against live GCP** —
`incident-agent serve` pulls from a subscription and `DataprocLogSource` reads
Cloud Logging, but neither has run against a real failed batch yet. The log
filters in `sources/dataproc.py` are a starting point, not a pinned answer;
`incident-agent log-filter <job-id>` prints them for checking in Logs Explorer.
A filter that matches nothing looks exactly like a job that produced no logs,
so verify before trusting it.

## Quick start

Requires Python 3.13 and [uv](https://docs.astral.sh/uv/).

**Inference runs on Gemini via Vertex AI** (`gemini-3.5-flash`, location
`global`), billing to `hack-team-treasury-geeks-2026` and authenticating with
Application Default Credentials:

```bash
gcloud auth application-default login
```

Claude on Vertex was the original plan but **Anthropic models are not enabled on
this project** — verified 2026-07-23: every `publishers/anthropic/models/*`
returns 404 across `global`, `us-east5` and `us-central1`, while
`publishers/google/models/gemini-3.5-flash` returns 200. The Claude path is
still wired and tested; set `INCIDENT_PROVIDER=claude` if an admin enables it.

```bash
uv sync

# Classify only — no repository access, ~$0.03
uv run incident-agent triage-only tests/fixtures/logs/schema_mismatch.log

# Classify + locate in the repo, but do not patch — ~$0.40
uv run incident-agent run \
  --log-file tests/fixtures/logs/schema_mismatch.log \
  --repo examples/broken-spark-job \
  --stop-after rootcause

# All four agents — prints the diff and PR body, pushes nothing
uv run incident-agent run \
  --log-file tests/fixtures/logs/schema_mismatch.log \
  --repo examples/broken-spark-job \
  --dry-run

# Consume from Pub/Sub (needs `uv sync --extra gcp` and GCP credentials)
uv run incident-agent serve --subscription spark-failures-sub
```

`examples/broken-spark-job` is a real PySpark job with a real bug: it reads
`event_ts` from a table whose column is `event_timestamp`. Its three tests fail
before the agent runs and pass after. `examples/fx-quote-normalizer` is the
second case — a Python UDF that calls `.strip()` on a nullable column — paired
with `tests/fixtures/logs/null_udf_fx.log`.

Drop `--dry-run` and set `GITHUB_TOKEN` to open actual draft PRs. `--provider
claude` switches to the Agent SDK; `--model` overrides the fix-stage model for
whichever provider is active.

## What it costs

Measured on the bundled fixture, full four-agent run on Gemini: **~48 seconds,
~95k input + ~1.6k output tokens.**

| Stage | Time | Steps | In | Out |
|---|---|---|---|---|
| classify | 7s | — | 4,110 | 370 |
| root cause | 12s | 4 | 18,481 | 318 |
| fix | 22s | 9 | 71,040 | 769 |
| narrate | 6s | — | 1,782 | 164 |

Every run prints this table, so spend is attributable per agent. **Vertex
reports tokens but not price**, so the cost column shows `—` rather than a
fabricated number; convert with current Gemini pricing if you need a figure.

For comparison, the same run on Claude Opus 4.8 took ~2.5 min and $0.74 — the
Claude path reports real cost because the Agent SDK returns it.

## How it decides

**Gate 1 — after classify.** All three must hold:

1. the class is code- or config-fixable (see `uv run incident-agent taxonomy`),
2. confidence ≥ `INCIDENT_CONFIDENCE_THRESHOLD` (default 0.7),
3. it cites at least one line from the log as evidence.

**Gate 2 — after root cause.** The agent must have *located* the cause: a named
file, an explained mechanism, and confidence ≥ `INCIDENT_ROOTCAUSE_THRESHOLD`.
`located: false` is a correct and useful outcome — the cause may be upstream,
the log too generic, or the deployed revision may differ from what was read.
Stopping there beats handing an unlocated hypothesis to an agent with write
access.

`missing_input`, `permission` and `transient` failures are reported and stopped
— the fix for those is upstream, in IAM, or a retry, and patching code would
only hide them. A verdict with no citation is downgraded to `unknown` in code,
not merely discouraged in the prompt.

**Verification has a baseline.** Before the fix agent runs, the repo's own test
suite runs once (`verify.baseline_failures`). A post-fix run that fails only on
tests that already failed is not a regression: it passes at tier 1, the PR is
flagged *weakly verified*, and the pre-existing failures are named. A new
failure still rejects the attempt. Without this, one unrelated broken test in
the target repo rejected every fix. `INCIDENT_VERIFY_BASELINE=false` turns it
off.

**Provider failures are not verdicts.** A Vertex 429/503 is retried with
backoff (`INCIDENT_GEMINI_MAX_RETRIES`); if it still fails, the run ends as
`error` and the Pub/Sub consumer nacks the message so it is redelivered. It
used to become an `unknown` classification that was acked — a lost incident.

## Where a finished run goes

Every finished run — PR opened or stopped at a gate — is handed to a notifier
and a run store (`notify.py`, `store.py`). Inspection runs (`triage-only`,
`--stop-after`) are not.

- **Notifier.** Default is a structured log line. `INCIDENT_NOTIFY_WEBHOOK_URL`
  switches to a Slack-compatible `{"text": ...}` POST with the outcome, class,
  confidence, located file, gate reason and PR URL.
- **Run store.** `INCIDENT_RUN_STORE=json` writes one `RunRecord` per run under
  `INCIDENT_RUN_STORE_DIR`; `firestore` writes to `INCIDENT_FIRESTORE_COLLECTION`
  (needs `--extra gcp`, and like the rest of the GCP path has not run live).
  Default is `none`.

Neither can change the outcome; a failing webhook is logged and the run still
finishes.

## Guardrails

On Gemini the tools are ours (`tools.py`), so enforcement lives in the
dispatcher — there is no path from a model-supplied argument to the filesystem
that skips a check. On Claude the same checks run in a `PreToolUse` hook, for
read tools as well as write and bash. The checks themselves are shared pure
functions in `guardrails.py`:

- **Reads** must resolve inside the worktree (after `~`, symlink and `../`
  resolution) and may not open `.git/**`, `.env*`, `secrets/**`, `credentials*`,
  key or PEM files. Build files and tests outside the edit scope stay readable.
- **Writes** must resolve inside the worktree and match `INCIDENT_EDITABLE_GLOBS`
  while missing `INCIDENT_PROTECTED_GLOBS`.
- **Bash** cannot run git, `gh`, cloud CLIs, network tools, package installers,
  or destructive commands — including via chaining, command substitution, `eval`,
  env-var prefixes or absolute paths — and cannot touch `.git/` or credential
  files.
- **The root-cause stage is structurally read-only**: it is never given the
  edit, write or command tools at all.

**What the bash check does not do** is read inside interpreters. `python -c`
can import `urllib` or `subprocess`, so the deny-list is a tripwire, not a
sandbox. Containment comes from two other properties, both tested:

- **No credential is reachable from the worktree or the agent's environment.**
  The GitHub token is never placed in a clone URL (git would persist it in
  `.git/config`); it travels per command as a `git -c http.extraheader`
  (`repo.auth_args`) for clone, `ls-remote` and push only. Every subprocess that
  runs repository code — the agent's shell *and* the verification step that
  runs the repo's tests — gets `guardrails.subprocess_env`: an allow-list
  (`PATH`, `JAVA_HOME`, locale) with a throwaway `$HOME` and no tokens, keys or
  ADC path. A test the fix agent writes runs with exactly what its shell had.
- **The orchestrator owns git.** It commits and pushes after the diff exists;
  the agent never runs it.

`tests/test_guardrails.py` covers the checks (including the `~` and `.git`
cases), `tests/test_tools.py` the dispatcher, and `tests/test_pr_push.py`
asserts against a real local remote that the token is absent from `.git/config`
and from git error messages.

Secrets are scrubbed from log text before it enters any prompt, PR body or
notification (`redact.py`), and `tests/test_redact.py` asserts on a seeded
fixture.

## Tests

```bash
uv run pytest              # unit tests, no API calls, ~6s
uv run pytest -m golden    # live classifier regression over the log corpus (costs money)
```

The golden set runs on whichever provider is configured — Gemini needs
Application Default Credentials, Claude needs the `claude` CLI — and is
skipped with a reason otherwise. Push behaviour (`tests/test_pr_push.py`) runs
against a real local bare repository with only `gh` faked. Provider retry and
the token ceiling are covered with the network faked
(`tests/test_gemini_provider.py`); notifications and the run store in
`tests/test_reporting.py`. CI (`.github/workflows/ci.yml`) runs lint and the
unit suite.

The golden set is the one that matters: it asserts the predicted class, the
auto-PR decision, and that every cited line number actually matches the log.
**Grow `tests/fixtures/logs/` from real captured failures** — that corpus is
what keeps triage honest as the taxonomy expands.

## Configuration

Copy `.env.example` to `.env`. Everything is overridable with an `INCIDENT_`
prefix:

| Setting | Default | Notes |
|---|---|---|
| `INCIDENT_TRIAGE_MODEL` / `_EFFORT` | `claude-opus-4-8` / `medium` | Agent 1. Set to `claude-haiku-4-5` to cut triage cost |
| `INCIDENT_ROOTCAUSE_MODEL` / `_EFFORT` | `claude-opus-4-8` / `high` | Agent 2. Reasons across log and code — not the place to economise |
| `INCIDENT_FIX_MODEL` / `_EFFORT` | `claude-opus-4-8` / `xhigh` | Agent 3. `xhigh` is the recommended tier for coding |
| `INCIDENT_NARRATE_MODEL` / `_EFFORT` | `claude-opus-4-8` / `medium` | Agent 4. Short output, no tools |
| `INCIDENT_CONFIDENCE_THRESHOLD` | `0.7` | Gate 1 — below this, notify instead of patching |
| `INCIDENT_ROOTCAUSE_THRESHOLD` | `0.7` | Gate 2 — below this, do not start the fix agent |
| `INCIDENT_SPARK_SUBDIR` | `incident-resolver/scala-spark-application` | Monorepo subtree the agent may edit; empty means the whole repo |
| `INCIDENT_MAX_FIX_ITERATIONS` | `3` | Fix → verify → fix attempts |
| `INCIDENT_VERIFY_BASELINE` | `true` | Run the repo's tests before the fix so pre-existing failures are not blamed on it |
| `INCIDENT_MAX_BUDGET_USD` | `5.0` | Per-incident ceiling, **Claude only** (enforced by the Agent SDK). Gemini is bounded by tokens, below |
| `INCIDENT_DRY_RUN` | `false` | Kill switch; same effect as `--dry-run` |
| `INCIDENT_PROVIDER` | `gemini` | `gemini` (Vertex) or `claude` (Agent SDK) |
| `INCIDENT_GEMINI_*_MODEL` | `gemini-3.5-flash` | Per agent: `CLASSIFY`, `ROOTCAUSE`, `FIX`, `NARRATE` |
| `INCIDENT_GEMINI_TEMPERATURE` | `0.2` | Low, but non-zero so a retry does not repeat the same edit |
| `INCIDENT_GEMINI_MAX_STEPS` | `40` | Tool-calling turns per agentic stage |
| `INCIDENT_GEMINI_MAX_STAGE_TOKENS` | `600000` | Cumulative input tokens per agentic stage — each step re-sends the history, so this is the spend ceiling |
| `INCIDENT_GEMINI_MAX_RETRIES` / `_RETRY_BACKOFF_S` | `3` / `1.0` | Retries on 429/5xx with exponential backoff |
| `INCIDENT_NOTIFY_WEBHOOK_URL` | unset | Slack-compatible webhook for finished runs; unset means log only |
| `INCIDENT_RUN_STORE` / `_RUN_STORE_DIR` | `none` / `.incident-agent/runs` | `none`, `json` or `firestore` |
| `INCIDENT_GCP_PROJECT` | `hack-team-treasury-geeks-2026` | Vertex project, and the Pub/Sub project |
| `INCIDENT_VERTEX_REGION` | `global` | Vertex location |

## Mapping a job to its repository

The agent cannot patch code it cannot find. Resolution order:

1. **Dataproc batch labels** (preferred, per-run accurate):
   ```bash
   gcloud dataproc batches submit pyspark jobs/daily_rollup.py \
     --labels repo=SidraShaikh1211--analytics-spark-jobs,git-sha=$(git rev-parse HEAD)
   ```
   GCP labels disallow `/`, so `org--name` expands to `org/name`.
2. `config/job_registry.yaml`, keyed by job-name prefix.

Unresolvable jobs stop at triage with a diagnosis rather than guessing.

## Known limitations

- **Verification needs the target repo's dependencies.** Tier 2 runs the repo's
  own pytest suite using whatever Python is on `PATH`. Locally that works
  because pyspark is a dev dependency here; the container has neither pyspark
  nor a JRE. See the note at the end of `Dockerfile` before enabling tier 2 in
  Cloud Run — otherwise every PR silently downgrades to "weakly verified".
- **The GCP path has never run.** `serve` and `DataprocLogSource` are written
  and unit-tested, but no real Dataproc failure has flowed through them. Expect
  the log filter to need adjusting on first contact.
- **The target Spark app does not exist yet.** `incident-resolver/scala-spark-application`
  is an empty placeholder, so `INCIDENT_SPARK_SUBDIR` scoping is untested
  against real code. The taxonomy covers Scala/JVM signatures and a Scala
  fixture is in the golden set, but that fixture is synthetic.
- **Dedupe is by branch name.** `find_existing_pr` lists open PRs and matches
  the fingerprint in `headRefName`; the fingerprint is also in the PR body and
  commit message. A branch left behind by a run that died between push and
  `gh pr create` is replaced on the next attempt. There is no cross-process
  lock, so two workers consuming the same failure simultaneously could race.
- **JVM verification needs `sbt` or `mvn` on PATH.** A Scala/Java change is
  compiled (tier 1) and `src/test` run (tier 2) when a build tool is present;
  otherwise the PR says plainly that nothing was verified. The container image
  has neither.
- **The bash deny-list does not inspect interpreter arguments.** See
  *Guardrails*: containment relies on the agent's environment and worktree
  holding no credentials, which is tested, rather than on the deny-list alone.
- **Triage can be confidently wrong.** A Spark stack trace often cannot
  distinguish a typo from genuine upstream schema drift — the bundled fixture is
  exactly that ambiguity, and the model correctly flags it rather than resolving
  it. Draft PRs and the confidence field exist because of this.

## Layout

```
src/incident_agent/
  cli.py            run | serve | triage-only | log-filter | taxonomy | config
  pipeline.py       orchestrates the four agents and the gates between them
  agents/
    base.py         shared Agent SDK plumbing + per-stage accounting
    classify.py     1. taxonomy match, no repo access
    rootcause.py    2. locate the cause, read-only repo access
    fix.py          3. patch and verify, write access (scoped)
    narrate.py      4. PR prose, no repo access
  guardrails.py     the safety boundary — pure checks, shared by both providers
  tools.py          Gemini's repo tools; enforces guardrails in its dispatcher
  providers/
    gemini.py       Vertex: structured_call + the function-calling agent loop
  ingest/
    message.py      Pub/Sub payload -> IncidentMessage (sink or hand-published)
    puller.py       pull-subscription worker, ack/nack/lease handling
  sources/
    base.py         the LogSource seam
    local.py        a captured log file
    dataproc.py     Cloud Logging + Dataproc API
  verify.py         tiered verification, with a pre-fix baseline
  repo.py           worktree lifecycle; all git operations; per-command auth
  pr.py             branch, commit, push, draft PR, dedupe by branch name
  notify.py         where finished runs are announced (log, webhook)
  store.py          where finished runs are kept (none, json, firestore)
  redact.py         secret scrubbing + failure fingerprinting
prompts/            taxonomy + one system prompt per agent + PR template
examples/           a deliberately broken PySpark job
tests/fixtures/     the captured-log corpus
```
