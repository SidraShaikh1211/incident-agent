"""Local-mode tests for the daily rollup.

The schema here mirrors the production `events` table as it exists today.
"""

from __future__ import annotations

import datetime as dt

import pytest
from jobs.daily_rollup import rollup
from pyspark.sql import SparkSession
from pyspark.sql.types import (
    DoubleType,
    StringType,
    StructField,
    StructType,
    TimestampType,
)

EVENTS_SCHEMA = StructType(
    [
        StructField("user_id", StringType(), False),
        StructField("session_id", StringType(), False),
        StructField("event_timestamp", TimestampType(), False),
        StructField("revenue", DoubleType(), True),
    ]
)


@pytest.fixture(scope="session")
def spark() -> SparkSession:
    session = (
        SparkSession.builder.appName("test_daily_rollup")
        .master("local[2]")
        .config("spark.sql.shuffle.partitions", "2")
        .config("spark.ui.enabled", "false")
        .getOrCreate()
    )
    yield session
    session.stop()


@pytest.fixture()
def events(spark: SparkSession):
    rows = [
        ("u1", "s1", dt.datetime(2026, 7, 20, 9, 0), 10.0),
        ("u1", "s1", dt.datetime(2026, 7, 20, 9, 5), 0.0),
        ("u1", "s2", dt.datetime(2026, 7, 20, 18, 0), 5.0),
        ("u2", "s3", dt.datetime(2026, 7, 20, 11, 0), 0.0),
        ("u1", "s4", dt.datetime(2026, 7, 21, 8, 0), 2.5),
    ]
    return spark.createDataFrame(rows, schema=EVENTS_SCHEMA)


def test_rollup_counts_events_per_user_per_day(events) -> None:
    result = {(r["user_id"], str(r["event_date"])): r for r in rollup(events).collect()}

    assert set(result) == {("u1", "2026-07-20"), ("u1", "2026-07-21"), ("u2", "2026-07-20")}
    assert result[("u1", "2026-07-20")]["event_count"] == 3
    assert result[("u2", "2026-07-20")]["event_count"] == 1


def test_rollup_counts_distinct_sessions(events) -> None:
    result = {(r["user_id"], str(r["event_date"])): r for r in rollup(events).collect()}

    assert result[("u1", "2026-07-20")]["session_count"] == 2
    assert result[("u1", "2026-07-21")]["session_count"] == 1


def test_rollup_sums_revenue(events) -> None:
    result = {(r["user_id"], str(r["event_date"])): r for r in rollup(events).collect()}

    assert result[("u1", "2026-07-20")]["revenue"] == pytest.approx(15.0)
    assert result[("u1", "2026-07-21")]["revenue"] == pytest.approx(2.5)
