"""Local-mode tests for the FX quote normaliser.

The schema here mirrors the production `fx_quotes_raw` table. `quote_ccy` is
nullable: two of the venue feeds omit it on indicative-only quotes.
"""

from __future__ import annotations

import datetime as dt

import pytest
from jobs.normalize_fx import normalise
from pyspark.sql import SparkSession
from pyspark.sql.types import (
    DoubleType,
    StringType,
    StructField,
    StructType,
    TimestampType,
)

QUOTES_SCHEMA = StructType(
    [
        StructField("quote_id", StringType(), False),
        StructField("base_ccy", StringType(), True),
        StructField("quote_ccy", StringType(), True),
        StructField("bid", DoubleType(), False),
        StructField("ask", DoubleType(), False),
        StructField("quoted_at", TimestampType(), False),
    ]
)


@pytest.fixture(scope="session")
def spark() -> SparkSession:
    session = (
        SparkSession.builder.appName("test_normalize_fx")
        .master("local[2]")
        .config("spark.sql.shuffle.partitions", "2")
        .config("spark.ui.enabled", "false")
        .getOrCreate()
    )
    yield session
    session.stop()


@pytest.fixture()
def quotes(spark: SparkSession):
    rows = [
        ("q1", "eur ", "usd", 1.0840, 1.0842, dt.datetime(2026, 7, 23, 9, 0)),
        ("q2", "GBP", " JPY", 198.10, 198.30, dt.datetime(2026, 7, 23, 9, 1)),
        # An indicative-only quote from the Nordics feed: no quote currency.
        ("q3", "SEK", None, 10.55, 10.61, dt.datetime(2026, 7, 23, 9, 2)),
        ("q4", "usd", "chf", 0.8790, 0.8794, dt.datetime(2026, 7, 23, 9, 3)),
    ]
    return spark.createDataFrame(rows, schema=QUOTES_SCHEMA)


def test_pair_is_canonicalised(quotes) -> None:
    result = {r["quote_id"]: r for r in normalise(quotes).collect()}

    assert result["q1"]["pair"] == "EUR/USD"
    assert result["q2"]["pair"] == "GBP/JPY"
    assert result["q4"]["pair"] == "USD/CHF"


def test_missing_currency_yields_a_null_pair(quotes) -> None:
    """A quote with no quote currency has no pair — it must not fail the job."""
    result = {r["quote_id"]: r for r in normalise(quotes).collect()}

    assert result["q3"]["pair"] is None


def test_mid_price_is_the_average_of_bid_and_ask(quotes) -> None:
    result = {r["quote_id"]: r for r in normalise(quotes).collect()}

    assert result["q1"]["mid_price"] == pytest.approx(1.0841)
    assert result["q3"]["mid_price"] == pytest.approx(10.58)
