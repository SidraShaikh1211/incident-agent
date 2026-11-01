"""Where finished runs are announced.

A run that stops at a gate is a diagnosis somebody has to read. On a laptop
that is the terminal. In ``serve`` mode nothing looks at stderr, so every
finished run — PR or not — is handed to a :class:`Notifier` as well.

The default writes a structured log line. Set ``INCIDENT_NOTIFY_WEBHOOK_URL``
to POST a Slack-compatible ``{"text": ...}`` payload instead; Slack, Google
Chat and most paging tools accept that shape.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Protocol

import structlog

from .config import Settings
from .models import RunRecord
from .redact import redact

log = structlog.get_logger(__name__)


class Notifier(Protocol):
    def notify(self, record: RunRecord) -> None:  # pragma: no cover - protocol
        ...


def format_notification(record: RunRecord) -> str:
    """Plain-text summary an on-call engineer can act on without the log."""
    head = f"[incident-agent] {record.outcome}"
    if record.job_name:
        head += f" — {record.job_name}"
    lines = [head, f"batch {record.batch_id} · fingerprint {record.fingerprint} · run {record.run_id}"]

    if record.verdict:
        v = record.verdict
        lines.append(f"class: {v.failure_class} @ {v.confidence:.2f} — {v.root_cause}")
    if record.root_cause:
        r = record.root_cause
        where = ", ".join(
            f"{loc.file_path}:{loc.line_number}" if loc.line_number else loc.file_path
            for loc in r.locations
        )
        lines.append(
            f"root cause: located={r.located} @ {r.confidence:.2f}" + (f" — {where}" if where else "")
        )
    if reason := record.extra.get("gate_reason"):
        lines.append(f"stopped: {reason}")
    if record.verify:
        lines.append(f"verified: tier {record.verify.tier} — {record.verify.tier_description}")
    if record.pr_url:
        lines.append(f"PR: {record.pr_url}")
    if record.error:
        lines.append(f"error: {record.error}")
    return redact("\n".join(lines))


class LogNotifier:
    """Default: one structured log event per finished run."""

    def notify(self, record: RunRecord) -> None:
        log.info(
            "incident_notification",
            outcome=record.outcome,
            job=record.job_name,
            pr=record.pr_url,
            text=format_notification(record),
        )


class WebhookNotifier:
    """POST ``{"text": ...}`` to an incoming-webhook URL."""

    def __init__(self, url: str, *, timeout_s: float = 10.0) -> None:
        self.url = url
        self.timeout_s = timeout_s

    def notify(self, record: RunRecord) -> None:
        body = json.dumps({"text": format_notification(record)}).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
            response.read()
        log.info("incident_notified", outcome=record.outcome, via="webhook")


def build_notifier(settings: Settings) -> Notifier:
    if settings.notify_webhook_url:
        return WebhookNotifier(settings.notify_webhook_url)
    return LogNotifier()
