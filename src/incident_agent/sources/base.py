"""The seam between local and GCP execution.

Everything downstream of a :class:`LogSource` is identical in both modes; this
protocol is the only thing that differs between ``--log-file`` and a live
Dataproc batch.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import FailureEvent


@runtime_checkable
class LogSource(Protocol):
    """Produces a normalised :class:`FailureEvent`."""

    def fetch(self) -> FailureEvent:  # pragma: no cover - protocol
        ...


def truncate_log(text: str, max_chars: int) -> str:
    """Keep the head and tail of a log, dropping the middle.

    Spark stack traces cluster at the end (the fatal error) while job config
    and schema dumps sit at the start; the repetitive task-level chatter in
    between is what blows the budget.
    """
    if len(text) <= max_chars:
        return text

    head = int(max_chars * 0.35)
    tail = max_chars - head
    dropped = len(text) - max_chars
    return (
        text[:head]
        + f"\n\n... [{dropped:,} characters of task-level output elided] ...\n\n"
        + text[-tail:]
    )
