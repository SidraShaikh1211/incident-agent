"""Pub/Sub payload parsing.

Both shapes that land on the topic must normalise identically, and anything we
cannot read must raise rather than be silently guessed at — acking a message we
misparsed loses a production failure.
"""

from __future__ import annotations

import base64
import json

import pytest

from incident_agent.ingest.message import UnparseableMessage, parse

# What a Cloud Logging sink actually publishes for a failed Serverless batch.
LOG_ENTRY = {
    "insertId": "abc123",
    "timestamp": "2026-07-22T03:00:29.123456Z",
    "severity": "ERROR",
    "resource": {
        "type": "cloud_dataproc_batch",
        "labels": {
            "project_id": "example-gcp-project",
            "location": "us-central1",
            "batch_id": "daily-rollup-20260722-0300",
        },
    },
    "jsonPayload": {"message": "AnalysisException: cannot resolve `event_ts`"},
}

# What a human publishes to rehearse the flow.
PLAIN = {
    "job_id": "daily-rollup-20260722-0300",
    "project": "example-gcp-project",
    "region": "us-central1",
    "job_name": "daily_rollup",
    "labels": {"repo": "db-hackathon--treasury-geeks"},
}


def test_parses_a_cloud_logging_sink_entry() -> None:
    msg = parse(json.dumps(LOG_ENTRY).encode())

    assert msg.job_id == "daily-rollup-20260722-0300"
    assert msg.project == "example-gcp-project"
    assert msg.region == "us-central1"
    assert msg.is_batch is True
    assert msg.occurred_at.year == 2026


def test_parses_a_cluster_job_entry_as_not_a_batch() -> None:
    entry = json.loads(json.dumps(LOG_ENTRY))
    entry["resource"]["type"] = "cloud_dataproc_job"
    entry["resource"]["labels"] = {
        "project_id": "p",
        "region": "europe-west2",
        "job_id": "job-99",
    }
    msg = parse(json.dumps(entry))

    assert msg.job_id == "job-99"
    assert msg.region == "europe-west2"
    assert msg.is_batch is False


def test_parses_a_hand_published_payload() -> None:
    msg = parse(json.dumps(PLAIN))

    assert msg.job_id == "daily-rollup-20260722-0300"
    assert msg.job_name == "daily_rollup"
    assert msg.labels["repo"] == "db-hackathon--treasury-geeks"


def test_both_shapes_agree_on_the_job() -> None:
    """The sink path and the manual path must be interchangeable for a demo."""
    from_sink = parse(json.dumps(LOG_ENTRY))
    from_hand = parse(json.dumps(PLAIN))

    assert from_sink.job_id == from_hand.job_id
    assert from_sink.project == from_hand.project
    assert from_sink.is_batch == from_hand.is_batch


def test_unwraps_a_push_style_envelope() -> None:
    envelope = {
        "message": {
            "data": base64.b64encode(json.dumps(PLAIN).encode()).decode(),
            "attributes": {"region": "asia-south1"},
        },
        "subscription": "projects/p/subscriptions/s",
    }
    msg = parse(json.dumps(envelope))
    assert msg.job_id == PLAIN["job_id"]


def test_attributes_supply_missing_fields() -> None:
    msg = parse(json.dumps({"job_id": "j-1"}), {"project": "p-from-attrs"})
    assert msg.project == "p-from-attrs"


@pytest.mark.parametrize(
    ("body", "expect_in_error"),
    [
        (b"not json at all", "not JSON"),
        (b"[1,2,3]", "not a JSON object"),
        (json.dumps({"project": "p"}).encode(), "no job_id"),
        (json.dumps({"job_id": "j"}).encode(), "no project"),
    ],
)
def test_unparseable_payloads_raise(body: bytes, expect_in_error: str) -> None:
    with pytest.raises(UnparseableMessage) as exc:
        parse(body)
    assert expect_in_error in str(exc.value)


def test_log_entry_without_a_job_id_names_the_labels_it_saw() -> None:
    """The error must be diagnosable from the log line alone."""
    entry = {"resource": {"type": "cloud_dataproc_batch", "labels": {"project_id": "p"}}}
    with pytest.raises(UnparseableMessage) as exc:
        parse(json.dumps(entry))
    assert "labels present" in str(exc.value)
    assert "project_id" in str(exc.value)
