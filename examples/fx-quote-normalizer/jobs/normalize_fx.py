"""Normalise raw FX quotes from several venue feeds into one canonical table.

Reads `fx_quotes_raw`, canonicalises the currency pair, computes a mid price,
and writes to `fx_quotes_curated`.
"""

from __future__ import annotations

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F
from pyspark.sql.types import StringType

SOURCE = "gs://treasury-raw/fx_quotes/"
TARGET = "gs://treasury-curated/fx_quotes/"


def build_session(app_name: str = "fx_normalize") -> SparkSession:
    return (
        SparkSession.builder.appName(app_name)
        .config("spark.sql.shuffle.partitions", "200")
        .getOrCreate()
    )


def canonical_pair(base: str, quote: str) -> str:
    """Venue feeds disagree on case and padding: 'eur ' and 'EUR' are the same."""
    return f"{base.strip().upper()}/{quote.strip().upper()}"


canonical_pair_udf = F.udf(canonical_pair, StringType())


def normalise(quotes: DataFrame) -> DataFrame:
    """One row per quote, with a canonical pair and a mid price."""
    return (
        quotes.withColumn(
            "pair", canonical_pair_udf(F.col("base_ccy"), F.col("quote_ccy"))
        )
        .withColumn("mid_price", (F.col("bid") + F.col("ask")) / 2)
        .select("quote_id", "pair", "mid_price", "quoted_at")
    )


def main() -> None:
    spark = build_session()
    quotes = spark.read.parquet(SOURCE)
    result = normalise(quotes)
    result.write.mode("overwrite").parquet(TARGET)
    spark.stop()


if __name__ == "__main__":
    main()
