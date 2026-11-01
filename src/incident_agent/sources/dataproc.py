"""Fetch a failed Dataproc job's logs from Cloud Logging.

Imports the GCP client libraries lazily so the local path (`FileLogSource`)
works without the `gcp` extra installed.

The log filters below are a starting point. Dataproc emits under two resource
types depending on how the job ran — `cloud_dataproc_batch` for Serverless
batches, `cloud_dataproc_job` for jobs on a cluster — and the exact shape must
be confirmed against a real failure before being trusted. See
:func:`describe_filters`, which prints what this module will query so you can
paste it into Logs Explorer and check it actually matches.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog

from ..models import FailureEvent
from .base import truncate_log

log = structlog.get_logger(__name__)

#: Serverless batches.
BATCH_FILTER = """
resource.type="cloud_dataproc_batch"
resource.labels.batch_id="{job_id}"
""".strip()

#: Jobs submitted to a Dataproc cluster.
JOB_FILTER = """
resource.type="cloud_dataproc_job"
resource.labels.job_id="{job_id}"
""".strip()

#: Driver output specifically, which is where the stack trace lands.
DRIVER_LOG_NAME = 'logName=~"dataproc.job.driver"'
CONTAINER_LOG_NAME = 'logName=~"dataproc.job.yarn.container"'


def describe_filters(job_id: str, region: str) -> str:
    """The queries this module will run, for pasting into Logs Explorer.

    Pinning these against a real failed batch is the first task of the GCP
    path — a filter that silently matches nothing looks identical to a job
    that produced no logs.
    """
    return (
        f"# Serverless batch\n{BATCH_FILTER.format(job_id=job_id)}\n\n"
        f"# Cluster job\n{JOB_FILTER.format(job_id=job_id)}\n\n"
        f"# Driver output only\n{DRIVER_LOG_NAME}\n"
        f"# YARN containers\n{CONTAINER_LOG_NAME}\n\n"
        f"# region: {region}"
    )


class DataprocLogSource:
    """Reads driver and container logs for one failed Dataproc job."""

    def __init__(
        self,
        *,
        job_id: str,
        project: str,
        region: str = "us-central1",
        job_name: str | None = None,
        labels: dict[str, str] | None = None,
        occurred_at: datetime | None = None,
        lookback_hours: int = 24,
        max_chars: int = 60_000,
        is_batch: bool = True,
    ) -> None:
        self.job_id = job_id
        self.project = project
        self.region = region
        self.job_name = job_name or job_id
        self.labels = labels or {}
        self.occurred_at = occurred_at
        self.lookback_hours = lookback_hours
        self.max_chars = max_chars
        self.is_batch = is_batch

    def fetch(self) -> FailureEvent:
        driver, container = self._read_logs()
        spark_conf, resolved_labels = self._read_job_metadata()

        merged_labels = {**resolved_labels, **self.labels}

        event = FailureEvent(
            batch_id=self.job_id,
            job_name=self.job_name,
            project=self.project,
            region=self.region,
            occurred_at=self.occurred_at or datetime.now(UTC),
            labels=merged_labels,
            driver_log=truncate_log(driver, self.max_chars),
            container_log=truncate_log(container, self.max_chars // 3),
            spark_conf=spark_conf,
            log_url=self._console_url(),
        )

        if not driver.strip() and not container.strip():
            log.warning(
                "no_logs_found",
                job_id=self.job_id,
                hint="the log filter may not match this job type; run "
                "`incident-agent log-filter` and check it in Logs Explorer",
            )
        return event

    # --- Cloud Logging ---

    def _read_logs(self) -> tuple[str, str]:
        from google.cloud import logging_v2

        client = logging_v2.Client(project=self.project)
        base = (BATCH_FILTER if self.is_batch else JOB_FILTER).format(job_id=self.job_id)

        since = (self.occurred_at or datetime.now(UTC)) - timedelta(hours=self.lookback_hours)
        window = f'timestamp>="{since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}"'

        driver = self._collect(client, f"{base}\n{window}\n{DRIVER_LOG_NAME}")
        container = self._collect(client, f"{base}\n{window}\n{CONTAINER_LOG_NAME}")

        # Some job types put everything on one stream; fall back to unfiltered.
        if not driver.strip():
            driver = self._collect(client, f"{base}\n{window}")

        return driver, container

    def _collect(self, client, filter_: str, limit: int = 4000) -> str:
        lines: list[str] = []
        try:
            for entry in client.list_entries(
                filter_=filter_, order_by="timestamp asc", max_results=limit
            ):
                lines.append(self._render(entry))
        except Exception as exc:  # noqa: BLE001 - a bad filter must not kill the run
            log.warning("log_fetch_failed", error=str(exc)[:300], filter=filter_[:200])
            return ""
        return "\n".join(lines)

    @staticmethod
    def _render(entry) -> str:
        payload = getattr(entry, "payload", None)
        if isinstance(payload, dict):
            text = payload.get("message") or payload.get("msg") or str(payload)
        else:
            text = str(payload) if payload is not None else ""
        ts = entry.timestamp.strftime("%y/%m/%d %H:%M:%S") if entry.timestamp else ""
        severity = getattr(entry, "severity", "") or ""
        return f"{ts} {severity} {text}".strip()

    # --- Dataproc API ---

    def _read_job_metadata(self) -> tuple[dict[str, str], dict[str, str]]:
        """Spark properties and job labels. Labels carry the repo mapping."""
        from google.cloud import dataproc_v1

        try:
            if self.is_batch:
                client = dataproc_v1.BatchControllerClient(
                    client_options={
                        "api_endpoint": f"{self.region}-dataproc.googleapis.com:443"
                    }
                )
                batch = client.get_batch(
                    name=f"projects/{self.project}/locations/{self.region}/batches/{self.job_id}"
                )
                props = dict(batch.runtime_config.properties or {})
                labels = dict(batch.labels or {})
                return props, labels

            client = dataproc_v1.JobControllerClient(
                client_options={"api_endpoint": f"{self.region}-dataproc.googleapis.com:443"}
            )
            job = client.get_job(
                project_id=self.project, region=self.region, job_id=self.job_id
            )
            spark_job = getattr(job, "pyspark_job", None) or getattr(job, "spark_job", None)
            props = dict(getattr(spark_job, "properties", {}) or {}) if spark_job else {}
            return props, dict(job.labels or {})
        except Exception as exc:  # noqa: BLE001 - metadata is a nice-to-have
            log.warning("job_metadata_unavailable", error=str(exc)[:300])
            return {}, {}

    def _console_url(self) -> str:
        kind = "batches" if self.is_batch else "jobs"
        return (
            f"https://console.cloud.google.com/dataproc/{kind}/{self.region}/"
            f"{self.job_id}/monitoring?project={self.project}"
        )
