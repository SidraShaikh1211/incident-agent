"""Tiered verification of an agent's patch.

A fix that imports cleanly is not a fix that works. The tier that actually
passed is reported prominently in the PR so a reviewer knows how much the
green checkmark is worth.

* Tier 1 — static: lint and import the changed Python modules, parse changed
  config files, compile a changed JVM project.
* Tier 2 — the repo's own test suite (for Spark repos, a ``local[2]`` session).
* Tier 3 — re-submit to Dataproc. Opt-in, not implemented in the local path.

Test code is untrusted. It comes from the cloned repository, and the fix agent
may have written some of it, so it runs with the same stripped environment as
the agent's shell: no tokens, no API keys, no ADC path.

A repository may already have failing tests before the agent touches it.
:func:`baseline_failures` captures that set first; a post-fix run that fails
only on those same tests is not a regression, and is reported as tier 1 with
the pre-existing failures named rather than as a failed fix.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import structlog

from .guardrails import subprocess_env
from .models import VerifyResult

log = structlog.get_logger(__name__)

MAX_OUTPUT_CHARS = 6000

#: ``-rfE`` prints one ``FAILED``/``ERROR`` line per test, which is what the
#: baseline comparison parses.
PYTEST = ["python", "-m", "pytest", "-q", "--no-header", "-rfE", "-p", "no:cacheprovider"]
_FAILED_LINE = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.M)


@contextmanager
def _scratch_home() -> Iterator[Path]:
    """A throwaway ``$HOME`` so tools cannot read the operator's dotfiles."""
    home = tempfile.mkdtemp(prefix="incident-agent-home-")
    try:
        yield Path(home)
    finally:
        shutil.rmtree(home, ignore_errors=True)


def _run(cmd: list[str], cwd: Path, *, env: dict[str, str], timeout: int = 900) -> tuple[int, str]:
    """Run a verification command with the sanitised environment."""
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, env=env
        )
    except subprocess.TimeoutExpired:
        return 124, f"$ {' '.join(cmd)}\nTIMEOUT after {timeout}s"
    except FileNotFoundError:
        return 127, f"$ {' '.join(cmd)}\ncommand not found"

    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, f"$ {' '.join(cmd)}\n{output.strip()}"


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    half = MAX_OUTPUT_CHARS // 2
    return text[:half] + "\n... [output elided] ...\n" + text[-half:]


def _parse_failures(output: str) -> set[str]:
    return set(_FAILED_LINE.findall(output))


# --- file classes -----------------------------------------------------------

CONFIG_SUFFIXES = {".yaml", ".yml", ".json", ".conf", ".properties"}
JVM_SUFFIXES = {".scala", ".java", ".sbt"}


def _is_config(rel: str) -> bool:
    return Path(rel).suffix.lower() in CONFIG_SUFFIXES


def _is_jvm(rel: str) -> bool:
    path = Path(rel)
    return path.suffix.lower() in JVM_SUFFIXES or path.name == "pom.xml"


def _check_config_syntax(path: Path) -> str | None:
    """Parse a config file the agent edited. Returns an error, or ``None``.

    Returns ``None`` for files this cannot check, so the caller can tell
    "parsed cleanly" from "not a config file" via :func:`_is_config`.
    """
    if not path.is_file() or not _is_config(path.name):
        return None

    suffix = path.suffix.lower()
    try:
        text = path.read_text(encoding="utf-8")
        if suffix in {".yaml", ".yml"}:
            import yaml

            yaml.safe_load(text)
        elif suffix == ".json":
            import json

            json.loads(text)
        else:
            # spark-defaults.conf / .properties: `key value` or `key=value`.
            for n, line in enumerate(text.splitlines(), start=1):
                stripped = line.strip()
                if not stripped or stripped.startswith(("#", "!")):
                    continue
                if "=" not in stripped and not re.match(r"^\S+\s+\S", stripped):
                    return f"line {n}: not a key/value pair: {stripped[:80]!r}"
    except Exception as exc:  # noqa: BLE001 - any parse failure is the answer
        return f"{type(exc).__name__}: {str(exc)[:300]}"
    return ""


def jvm_build(worktree: Path) -> tuple[list[str], list[str]] | None:
    """``(compile, test)`` commands for the JVM build tool present, or ``None``."""
    if (worktree / "build.sbt").is_file() and shutil.which("sbt"):
        return ["sbt", "-batch", "compile"], ["sbt", "-batch", "test"]
    if (worktree / "pom.xml").is_file() and shutil.which("mvn"):
        return ["mvn", "-q", "-B", "compile"], ["mvn", "-q", "-B", "test"]
    return None


def has_tests(worktree: Path) -> bool:
    if (worktree / "tests").is_dir():
        return True
    return any(worktree.glob("test_*.py")) or any(worktree.glob("**/test_*.py"))


def _has_jvm_tests(worktree: Path) -> bool:
    return (worktree / "src" / "test").is_dir()


# --- entry points -----------------------------------------------------------


def baseline_failures(worktree: Path) -> set[str] | None:
    """Tests that fail *before* the agent touches anything.

    ``None`` means no suite ran (none present, pytest missing, or the run
    crashed in a way that named no tests), so no baseline can be claimed.
    """
    if not has_tests(worktree):
        return None
    with _scratch_home() as home:
        code, out = _run(PYTEST, worktree, env=subprocess_env(home=home))
    if code == 127:
        return None
    failures = _parse_failures(out)
    # Exit 5 is "no tests collected"; a non-zero exit with nothing attributable
    # (an internal pytest error, say) tells us nothing about individual tests.
    if code not in (0, 5) and not failures:
        log.warning("baseline_unattributable", exit_code=code)
        return None
    log.info("baseline_tests", failing=len(failures))
    return failures


def verify(
    worktree: Path,
    changed_files: list[str],
    *,
    run_tests: bool = True,
    baseline: set[str] | None = None,
) -> VerifyResult:
    """Run the highest tier available and report which one passed."""
    with _scratch_home() as home:
        return _verify(
            worktree,
            changed_files,
            run_tests=run_tests,
            baseline=baseline,
            env=subprocess_env(home=home),
        )


def _verify(
    worktree: Path,
    changed_files: list[str],
    *,
    run_tests: bool,
    baseline: set[str] | None,
    env: dict[str, str],
) -> VerifyResult:
    transcript: list[str] = []
    commands: list[str] = []

    def failed(tier: int) -> VerifyResult:
        return VerifyResult(
            tier=tier, passed=False, output=_truncate("\n\n".join(transcript)),
            commands_run=commands,
        )

    # --- Tier 1: static ---
    py_files = [f for f in changed_files if f.endswith(".py")]

    if py_files and shutil.which("ruff"):
        code, out = _run(["ruff", "check", *py_files], worktree, env=env)
        transcript.append(out)
        commands.append("ruff check")
        if code not in (0, 127):
            return failed(0)

    for rel in py_files:
        code, out = _run(["python", "-m", "py_compile", rel], worktree, env=env, timeout=60)
        transcript.append(out)
        commands.append(f"py_compile {rel}")
        if code != 0:
            return failed(0)

    # Config-class fixes (executor_oom, driver_oom, shuffle_skew) change .conf,
    # .yaml or .properties files. Linting Python says nothing about those, so
    # parse them — otherwise a config PR claims verification it never had.
    for rel in changed_files:
        if reason := _check_config_syntax(worktree / rel):
            transcript.append(f"$ parse {rel}\n{reason}")
            commands.append(f"parse {rel}")
            return failed(0)
        elif reason is not None or _is_config(rel):
            transcript.append(f"$ parse {rel}\nOK")
            commands.append(f"parse {rel}")

    # Scala/Java fixes: compile. Without a build tool on PATH nothing runs and
    # the result honestly says so.
    jvm_changed = any(_is_jvm(f) for f in changed_files)
    jvm = jvm_build(worktree) if jvm_changed else None
    if jvm:
        compile_cmd, _ = jvm
        code, out = _run(compile_cmd, worktree, env=env)
        transcript.append(out)
        commands.append(" ".join(compile_cmd[:2]))
        if code != 0:
            return failed(0)
    elif jvm_changed:
        log.warning("jvm_toolchain_unavailable", changed=changed_files)

    # Nothing actually ran — claiming "static checks passed" here would be a
    # false statement on the PR a human uses to decide whether to merge.
    if not commands:
        log.warning("verify_nothing_ran", changed=changed_files)
        return VerifyResult(
            tier=0,
            passed=True,
            output=(
                "No verification tooling was available for these files. "
                "Nothing about this change has been checked."
            ),
            commands_run=[],
        )

    tier = 1
    log.info("verify_tier1_passed", files=len(py_files), commands=commands)

    # --- Tier 2: the repo's tests ---
    test_cmd: list[str] | None = None
    label = ""
    if run_tests and has_tests(worktree):
        test_cmd, label = PYTEST, "pytest -q"
    elif run_tests and jvm and _has_jvm_tests(worktree):
        test_cmd, label = jvm[1], " ".join(jvm[1][:2])

    if test_cmd is None:
        log.info("verify_no_tests_found")
    else:
        code, out = _run(test_cmd, worktree, env=env)
        transcript.append(out)
        commands.append(label)
        if code == 127:
            log.info("verify_test_runner_unavailable", command=label)
        elif code != 0:
            failures = _parse_failures(out)
            if baseline is not None and failures and failures <= baseline:
                # Not a regression: these failed before the agent changed anything.
                # Stay at tier 1 so the PR is flagged weakly verified and names them.
                note = (
                    f"{len(failures)} test(s) failed before this change and still fail; "
                    "no new failures were introduced:\n" + "\n".join(sorted(failures))
                )
                transcript.append(note)
                log.warning("verify_preexisting_failures", count=len(failures))
            else:
                return failed(1)
        else:
            tier = 2
            log.info("verify_tier2_passed")

    return VerifyResult(
        tier=tier,
        passed=True,
        output=_truncate("\n\n".join(transcript)),
        commands_run=commands,
    )
