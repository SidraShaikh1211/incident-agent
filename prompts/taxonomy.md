# Spark failure taxonomy

Classify a failure into exactly one class. Match on the **primary** error — the
one that caused the job to abort — not on incidental warnings that appear
earlier in the log.

Jobs may be **PySpark or Scala/Java**. The same failure class looks different in
each, and the signatures below list both. Do not treat the absence of a Python
traceback as evidence that there is no user-code bug — in a Scala job the
equivalent signal is a frame naming the job's own package.

| Class | Signature | fix_kind | Notes |
|---|---|---|---|
| `schema_mismatch` | `AnalysisException: cannot resolve '<col>' given input columns`; `UNRESOLVED_COLUMN.WITH_SUGGESTION`; `Column ... does not exist`; `UnresolvedAttribute`. **Scala also:** `org.apache.spark.sql.AnalysisException` at `CheckAnalysis.failUnresolvedAttribute`; a `Dataset.as[T]` decode failing on a missing field; `No such struct field` | `code` | A referenced column is absent from the input DataFrame. Distinguish a typo in the job (fixable here) from genuine upstream schema drift (report, may still be fixable by adapting the read). |
| `type_coercion` | `Cannot cast X to Y`; `NumberFormatException`; `SparkUpgradeException` on date parsing; `AnalysisException: due to data type mismatch`. **Scala also:** `ClassCastException` between `java.lang.*` boxed types or Spark `Row` accessors; `scala.MatchError` on an unexpected type; `Encoder` derivation errors | `code` | An expression, cast or comparison mixes incompatible types. |
| `null_handling` | `NullPointerException` with a UDF/lambda frame in the stack; `None` propagating into an arithmetic or string op inside a Python UDF. **Scala also:** an NPE or `NoSuchElementException: None.get` in a frame naming the job's own package, or inside `$anonfun$`/`SparkPlan$$Lambda` closures; `Option.get` on an empty Option | `code` | Missing null guard in user code. Requires a **user-code** frame in the trace — the job's own package name, or a UDF/closure it defined. A bare NPE entirely inside `org.apache.spark.*` internals is not this class. |
| `bad_join` | `BroadcastTimeout`; `Cannot broadcast the table that is larger than`; detected cartesian product; `SparkException: Exception thrown in awaitResult` under a broadcast exchange. **Scala also:** `Detected implicit cartesian product for INNER join`; an `Ambiguous reference to fields` on a self-join | `code` | Join strategy or join condition is wrong. |
| `executor_oom` | `Container killed by YARN for exceeding memory limits`; `ExecutorLostFailure ... exited caused by one of the running tasks`; `Consider boosting spark.yarn.executor.memoryOverhead`; `exit status: 143` | `config` | Executor memory or overhead is too small for the partition size. |
| `driver_oom` | `java.lang.OutOfMemoryError: Java heap space` or `GC overhead limit exceeded` with a driver-side frame; OOM immediately after `collect()`, `toPandas()`, `Dataset.collect()` or a large broadcast | `config` | Driver heap exhausted. Often collecting too much to the driver — say so in the root cause even though the immediate fix is a config bump. |
| `shuffle_skew` | `FetchFailedException`; `MetadataFetchFailedException`; `Missing an output location for shuffle`; `Total size of serialized results ... is bigger than spark.driver.maxResultSize`; a single task in a stage running orders of magnitude longer than its peers | `config` | Shuffle partition count or key distribution. |
| `missing_input` | `Path does not exist`; `PATH_NOT_FOUND`; `FileNotFoundException` on an input URI; `Table or view not found`; `TABLE_OR_VIEW_NOT_FOUND` | `data` | Upstream data has not landed. **Not fixable by a code change** — notify only. |
| `permission` | `403`; `AccessDenied`; `PERMISSION_DENIED`; `does not have storage.objects.get access` | `infra` | IAM problem. **Notify only.** |
| `transient` | Node preemption; `Executor ... was lost` with no OOM signature; quota errors with a retry hint; spot-VM reclamation | `none` | Retrying would likely succeed. **Notify only.** |
| `unknown` | Anything you cannot place with confidence | `none` | Prefer `unknown` over a confident wrong guess. |

## Rules

1. **Cite evidence.** Every verdict must reference at least one line from the
   supplied log, by its 1-based line number, quoted verbatim. A verdict with no
   evidence is discarded and downgraded to `unknown`.
2. **Primary error only.** Spark logs contain cascading failures — an
   `ExecutorLostFailure` following an OOM is a symptom of `executor_oom`, not a
   separate `transient` failure. Trace back to the first fatal cause. Spark
   wraps many failures in a generic `AnalysisException: Job aborted` or
   `SparkException: Job aborted due to stage failure`; that wrapper is never
   the class — the `403`, `Path does not exist` or `cannot resolve` beneath
   it is.
3. **Confidence is a real number, not a formality.** Use < 0.7 when the log is
   ambiguous or truncated, or when two classes both fit. Under-confidence costs
   a notification; over-confidence costs a wrong PR that a human has to catch.
4. **`data`, `infra` and `none` never produce a PR.** If the true fix is
   upstream or in IAM, say so plainly rather than inventing a code change that
   masks it.
