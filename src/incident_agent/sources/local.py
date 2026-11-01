"""Read a failure from a file or stdin. No GCP dependency."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from ..models import FailureEvent
from .base import truncate_log


class FileLogSource:
    """A captured Spark driver log on disk (or ``-`` for stdin).

    Optional front-matter lets a fixture carry its own metadata::

        # batch-id: my-batch-001
        # job-name: daily_rollup
        # repo: org/spark-jobs
        # spark.executor.memory: 4g
    """

    _FRONT_MATTER = re.compile(r"^#\s*([A-Za-z0-9_.\-]+)\s*:\s*(.+?)\s*$")

    def __init__(
        self,
        path: str | Path,
        *,
        batch_id: str | None = None,
        job_name: str | None = None,
        max_chars: int = 60_000,
    ) -> None:
        self.path = path
        self.batch_id = batch_id
        self.job_name = job_name
        self.max_chars = max_chars

    def fetch(self) -> FailureEvent:
        if str(self.path) == "-":
            raw = sys.stdin.read()
            source_name = "stdin"
        else:
            p = Path(self.path)
            if not p.exists():
                raise FileNotFoundError(f"log file not found: {p}")
            raw = p.read_text(encoding="utf-8", errors="replace")
            source_name = p.stem

        meta, body = self._split_front_matter(raw)

        labels = {k: v for k, v in meta.items() if "." not in k}
        spark_conf = {k: v for k, v in meta.items() if k.startswith("spark.")}

        return FailureEvent(
            batch_id=self.batch_id or meta.get("batch-id") or source_name,
            job_name=self.job_name or meta.get("job-name") or source_name,
            labels=labels,
            spark_conf=spark_conf,
            driver_log=truncate_log(body, self.max_chars),
        )

    def _split_front_matter(self, raw: str) -> tuple[dict[str, str], str]:
        meta: dict[str, str] = {}
        lines = raw.splitlines()
        idx = 0
        for line in lines:
            if not line.strip():
                idx += 1
                continue
            m = self._FRONT_MATTER.match(line)
            if not m:
                break
            meta[m.group(1).strip()] = m.group(2).strip()
            idx += 1
        return meta, "\n".join(lines[idx:]).lstrip("\n")
