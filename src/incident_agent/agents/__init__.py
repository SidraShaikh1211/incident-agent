"""The four agents, in pipeline order.

1. :mod:`classify`   — taxonomy match from the log alone. No repo access.
2. :mod:`rootcause`  — locate the cause in source. Read-only repo access.
3. :mod:`fix`        — patch and verify. The only stage with write access.
4. :mod:`narrate`    — compose the PR prose. No repo access.

Each stage gates the next: a notify-only class never reaches the repo, and an
unlocated root cause never reaches an agent that can edit files.
"""

from .classify import classify
from .fix import apply_fix, summarise_violations
from .narrate import narrate
from .rootcause import find_root_cause

__all__ = [
    "classify",
    "find_root_cause",
    "apply_fix",
    "narrate",
    "summarise_violations",
]
