"""Where finished :class:`~incident_agent.models.RunRecord` objects are kept.

The record is the audit trail: what the classifier said, what was located,
what changed, how it was verified, and what it cost. Nothing else retains it.

* ``none`` (default) — nothing is written; the CLI prints the record.
* ``json`` — one file per run under ``INCIDENT_RUN_STORE_DIR``.
* ``firestore`` — one document per run in ``INCIDENT_FIRESTORE_COLLECTION``.
  Needs the ``gcp`` extra. Written against the client API but, like the rest
  of the GCP path, not yet exercised against a live project.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

import structlog

from .config import Settings
from .models import RunRecord

log = structlog.get_logger(__name__)


class RunStore(Protocol):
    def save(self, record: RunRecord) -> None:  # pragma: no cover - protocol
        ...


class NullRunStore:
    def save(self, record: RunRecord) -> None:
        return None


class JsonFileRunStore:
    """``<dir>/<run_id>.json`` per run. Good enough for a laptop or a volume."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def _path(self, run_id: str) -> Path:
        return self.directory / f"{run_id}.json"

    def save(self, record: RunRecord) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self._path(record.run_id).write_text(record.model_dump_json(indent=2), encoding="utf-8")
        log.info("run_stored", run_id=record.run_id, path=str(self._path(record.run_id)))

    def load(self, run_id: str) -> RunRecord:
        return RunRecord.model_validate_json(self._path(run_id).read_text(encoding="utf-8"))

    def recent(self, limit: int = 20) -> list[RunRecord]:
        if not self.directory.is_dir():
            return []
        paths = sorted(self.directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        return [RunRecord.model_validate_json(p.read_text(encoding="utf-8")) for p in paths[:limit]]


class FirestoreRunStore:
    """One document per run. The client is imported lazily (``gcp`` extra)."""

    def __init__(self, project: str, collection: str) -> None:
        self.project = project
        self.collection = collection

    def save(self, record: RunRecord) -> None:
        from google.cloud import firestore

        client = firestore.Client(project=self.project)
        data = json.loads(record.model_dump_json())
        client.collection(self.collection).document(record.run_id).set(data)
        log.info("run_stored", run_id=record.run_id, collection=self.collection)


def build_store(settings: Settings) -> RunStore:
    kind = (settings.run_store or "none").lower()
    if kind == "json":
        return JsonFileRunStore(settings.run_store_dir)
    if kind == "firestore":
        if not settings.gcp_project:
            raise ValueError("run_store=firestore needs INCIDENT_GCP_PROJECT")
        return FirestoreRunStore(settings.gcp_project, settings.firestore_collection)
    if kind != "none":
        log.warning("unknown_run_store", value=settings.run_store, using="none")
    return NullRunStore()
