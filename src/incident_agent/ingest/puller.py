"""Pull-subscription worker: the agent picks failures off the queue itself.

Chosen over a push subscription deliberately. A push endpoint must ack within
600 seconds and needs a public HTTPS address; an incident run takes minutes and
should not be racing an ack deadline. Pulling means the same worker binary runs
unchanged on a laptop and on Cloud Run, with no endpoint to expose.

Ack semantics:

* processed (PR opened, or a gate stopped it with a diagnosis) → **ack**
* unparseable payload → **ack** and log loudly; redelivering it forever just
  fills the queue, and the DLQ is the right home for it
* infrastructure failure mid-run → **nack** so it is retried

The subscription's ack deadline is extended while a run is in flight, so a
long agent session does not cause a duplicate delivery.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from concurrent.futures import TimeoutError as FuturesTimeout

import structlog

from ..config import Settings
from ..models import RunRecord
from ..pipeline import Outcome, run_incident
from ..sources.dataproc import DataprocLogSource
from .message import IncidentMessage, UnparseableMessage, parse

log = structlog.get_logger(__name__)


class PubSubPuller:
    """Long-running consumer that drives one incident per message."""

    def __init__(
        self,
        settings: Settings,
        *,
        subscription: str,
        project: str | None = None,
        max_messages: int = 1,
        ack_deadline_s: int = 600,
    ) -> None:
        self.settings = settings
        self.project = project or settings.gcp_project
        if not self.project:
            raise ValueError("gcp_project is required (set INCIDENT_GCP_PROJECT)")
        self.subscription = subscription
        # One at a time by default: an incident run is expensive and the
        # guardrails assume a single worktree per process.
        self.max_messages = max_messages
        self.ack_deadline_s = ack_deadline_s
        self._stop = asyncio.Event()

    @property
    def subscription_path(self) -> str:
        from google.cloud import pubsub_v1

        client = pubsub_v1.SubscriberClient()
        return client.subscription_path(self.project, self.subscription)

    async def run_forever(self) -> None:
        """Poll until interrupted. Each message becomes one incident run."""
        from google.cloud import pubsub_v1

        subscriber = pubsub_v1.SubscriberClient()
        path = subscriber.subscription_path(self.project, self.subscription)

        self._install_signal_handlers()
        log.info("puller_started", subscription=path, max_messages=self.max_messages)

        try:
            while not self._stop.is_set():
                received = await asyncio.to_thread(self._pull_once, subscriber, path)
                if not received:
                    await self._sleep_or_stop(5.0)
                    continue

                for ack_id, message in received:
                    if self._stop.is_set():
                        # Give it back rather than starting work we cannot finish.
                        await asyncio.to_thread(
                            subscriber.modify_ack_deadline,
                            request={
                                "subscription": path,
                                "ack_ids": [ack_id],
                                "ack_deadline_seconds": 0,
                            },
                        )
                        break
                    await self._handle(subscriber, path, ack_id, message)
        finally:
            subscriber.close()
            log.info("puller_stopped")

    # --- internals ---

    def _pull_once(self, subscriber, path: str):
        try:
            response = subscriber.pull(
                request={"subscription": path, "max_messages": self.max_messages},
                timeout=20.0,
            )
        except FuturesTimeout:
            return []
        except Exception as exc:  # noqa: BLE001 - keep the loop alive
            log.warning("pull_failed", error=str(exc)[:300])
            return []
        return [(m.ack_id, m.message) for m in response.received_messages]

    async def _handle(self, subscriber, path: str, ack_id: str, message) -> None:
        attributes = dict(message.attributes or {})

        try:
            incident = parse(message.data, attributes)
        except UnparseableMessage as exc:
            # Ack: redelivering a message we cannot read achieves nothing.
            log.error(
                "message_unparseable",
                error=str(exc),
                body=message.data[:400].decode("utf-8", errors="replace"),
            )
            await asyncio.to_thread(
                subscriber.acknowledge, request={"subscription": path, "ack_ids": [ack_id]}
            )
            return

        log.info(
            "incident_received",
            job_id=incident.job_id,
            project=incident.project,
            region=incident.region,
        )

        keepalive = asyncio.create_task(self._extend_deadline(subscriber, path, ack_id))
        try:
            record = await self._process(incident)
            await asyncio.to_thread(
                subscriber.acknowledge, request={"subscription": path, "ack_ids": [ack_id]}
            )
            log.info(
                "incident_acked",
                job_id=incident.job_id,
                outcome=record.outcome,
                pr=record.pr_url,
                cost_usd=round(record.total_cost_usd, 4),
            )
        except Exception:  # noqa: BLE001 - infrastructure failure, retry it
            log.exception("incident_failed_nacking", job_id=incident.job_id)
            await asyncio.to_thread(
                subscriber.modify_ack_deadline,
                request={
                    "subscription": path,
                    "ack_ids": [ack_id],
                    "ack_deadline_seconds": 0,
                },
            )
        finally:
            keepalive.cancel()

    async def _process(self, incident: IncidentMessage) -> RunRecord:
        source = DataprocLogSource(
            job_id=incident.job_id,
            project=incident.project,
            region=incident.region,
            job_name=incident.job_name,
            labels=incident.labels,
            occurred_at=incident.occurred_at,
            is_batch=incident.is_batch,
            max_chars=self.settings.max_log_chars,
        )
        record = await run_incident(source, self.settings)
        if record.outcome == Outcome.ERROR:
            # Surface as an exception so the caller nacks and Pub/Sub retries.
            raise RuntimeError(record.error or "incident run failed")
        return record

    async def _extend_deadline(self, subscriber, path: str, ack_id: str) -> None:
        """Hold the lease while a run is in flight, so it is not redelivered."""
        interval = max(self.ack_deadline_s // 3, 30)
        try:
            while True:
                await asyncio.sleep(interval)
                await asyncio.to_thread(
                    subscriber.modify_ack_deadline,
                    request={
                        "subscription": path,
                        "ack_ids": [ack_id],
                        "ack_deadline_seconds": self.ack_deadline_s,
                    },
                )
                log.debug("ack_deadline_extended", seconds=self.ack_deadline_s)
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            log.warning("ack_extend_failed", error=str(exc)[:200])

    async def _sleep_or_stop(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):  # Windows
                loop.add_signal_handler(sig, self._stop.set)
