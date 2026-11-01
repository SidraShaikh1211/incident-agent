"""Daily rollup of raw clickstream events into per-user session metrics.

Reads the `events` table, aggregates per user per day, writes to `user_daily`.
"""

from __future__ import annotations

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F


def build_session(app_name: str = "daily_rollup") -> SparkSession:
    return (
        SparkSession.builder.appName(app_name)
        .config("spark.sql.shuffle.partitions", "200")
        .getOrCreate()
    )


def rollup(events: DataFrame) -> DataFrame:
    """Aggregate raw events into one row per user per day."""
    return (
        events.withColumn("event_date", F.to_date(F.col("event_ts")))
        .groupBy("user_id", "event_date")
        .agg(
            F.count("*").alias("event_count"),
            F.countDistinct("session_id").alias("session_count"),
            F.sum("revenue").alias("revenue"),
        )
        .orderBy("user_id", "event_date")
    )


def main() -> None:
    spark = build_session()
    events = spark.read.parquet("gs://analytics-raw/events/")
    result = rollup(events)
    result.write.mode("overwrite").parquet("gs://analytics-curated/user_daily/")
    spark.stop()


if __name__ == "__main__":
    main()
