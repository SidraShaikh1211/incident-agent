"""Parsing the Pub/Sub payload into something the pipeline can act on.

Two shapes arrive on the same topic:

* A **Cloud Logging sink** entry — the real trigger. Deeply nested, and the
  useful identifiers live in `resource.labels`.
* A **hand-published** JSON object — used to rehearse the flow without waiting
  for a real failure. Flat and obvious.

Both normalise to :class:`IncidentMessage`. Anything unparseable is reported
rather than guessed at, because acking a message we did not understand loses a
production failure silently.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Any

import structlog
from pydantic import BaseModel, Field

log = structlog.get_logger(__name__)


class IncidentMessage(BaseModel):
    """A failed Spark job, as identified from a Pub/Sub message."""

    job_id: str
    project: str
    region: str = "us-central1"
    job_name: str | None = None
    is_batch: bool = True
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    labels: dict[str, str] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict, exclude=True)


class UnparseableMessage(ValueError):
    """The payload carried no job identifier we recognise."""


def parse(data: bytes | str, attributes: dict[str, str] | None = None) -> IncidentMessage:
    """Normalise a Pub/Sub message body into an :class:`IncidentMessage`."""
    attributes = attributes or {}

    text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise UnparseableMessage(f"body is not JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise UnparseableMessage("body is not a JSON object")

    # A push-style envelope wraps the real entry in `message.data` (base64).
    if "message" in payload and isinstance(payload["message"], dict):
        inner = payload["message"]
        if inner.get("data"):
            decoded = base64.b64decode(inner["data"]).decode("utf-8", errors="replace")
            return parse(decoded, inner.get("attributes"))

    if _looks_like_log_entry(payload):
        return _from_log_entry(payload, attributes)
    return _from_plain(payload, attributes)


def _looks_like_log_entry(payload: dict[str, Any]) -> bool:
    return "resource" in payload and isinstance(payload.get("resource"), dict)


def _from_log_entry(entry: dict[str, Any], attributes: dict[str, str]) -> IncidentMessage:
    resource = entry.get("resource") or {}
    labels = {str(k): str(v) for k, v in (resource.get("labels") or {}).items()}
    rtype = resource.get("type", "")

    job_id = (
        labels.get("batch_id")
        or labels.get("job_id")
        or labels.get("session_id")
        or attributes.get("job_id")
    )
    if not job_id:
        raise UnparseableMessage(
            f"log entry of type {rtype!r} has no batch_id/job_id in resource.labels; "
            f"labels present: {sorted(labels)}"
        )

    project = labels.get("project_id") or attributes.get("project") or ""
    if not project:
        raise UnparseableMessage("log entry has no project_id")

    occurred = _parse_ts(entry.get("timestamp")) or datetime.now(UTC)

    return IncidentMessage(
        job_id=job_id,
        project=project,
        region=labels.get("region") or labels.get("location") or "us-central1",
        job_name=attributes.get("job_name"),
        is_batch="batch" in rtype,
        occurred_at=occurred,
        # Dataproc labels (which carry the repo mapping) are fetched from the
        # Dataproc API later — resource.labels here are Cloud Logging's own.
        labels={},
        raw=entry,
    )


def _from_plain(payload: dict[str, Any], attributes: dict[str, str]) -> IncidentMessage:
    job_id = (
        payload.get("job_id")
        or payload.get("batch_id")
        or attributes.get("job_id")
        or attributes.get("batch_id")
    )
    if not job_id:
        raise UnparseableMessage(
            f"no job_id/batch_id in payload; keys present: {sorted(payload)}"
        )

    project = payload.get("project") or payload.get("project_id") or attributes.get("project")
    if not project:
        raise UnparseableMessage("no project in payload")

    labels = payload.get("labels") or {}
    if not isinstance(labels, dict):
        labels = {}

    return IncidentMessage(
        job_id=str(job_id),
        project=str(project),
        region=str(payload.get("region") or attributes.get("region") or "us-central1"),
        job_name=payload.get("job_name"),
        is_batch=bool(payload.get("is_batch", True)),
        occurred_at=_parse_ts(payload.get("occurred_at")) or datetime.now(UTC),
        labels={str(k): str(v) for k, v in labels.items()},
        raw=payload,
    )


def _parse_ts(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
