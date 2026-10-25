---
description: Intervention insertion and query latency as the database grows through population milestones
---

# Record scalability

One execution grows a synthetic intervention registry through a list of target
populations. At each target it refreshes PostgreSQL statistics, times seven
queries, captures database facts, and publishes an observation. The parent
uploads completed observations while the experiment continues. No reset happens
automatically.

Results live under `Load Benchmarks / record_scalability`. Historical
`experiment_002` runs and analyses remain under their original experiment.

## Run

Use [example.yml](example.yml) for a complete config containing all defaults.
Copy it into the ignored `config/` folder and adjust the populations:

```yaml
experiment: experiments/record_scalability
n_rows: [1000, 10000, 100000]
```

From the repository root, run locally:

```sh
uv run python benchmarks/run_benchmark.py experiments/record_scalability/config/my_run.yml
```

Or run on Modal in the `Benchmarks` environment, with the `lamin-benchmarks`
secret containing `LAMIN_API_KEY`:

```sh
uv run --with modal python -m modal run --env Benchmarks benchmarks/modal_benchmarks.py \
  --config experiments/record_scalability/config/my_run.yml --region eu-west
```

The tracking instance defaults to `laminlabs/lamindb-benchmarks`; the load target
defaults to `laminlabs/lamindb-load-test`. Override them with
`LAMIN_CURRENT_INSTANCE` and `LAMIN_LOAD_INSTANCE` for a local launch. The Modal
image sets these defaults explicitly. The targets must differ, and the API key
must have the permissions needed for insertion and root-role maintenance.

For an intentionally fresh population, reset separately **before** launching:

```sh
uv run python benchmarks/reset.py --yes
```

Reset is untracked maintenance. It removes the load instance's benchmark data.

## Workload and configuration

An intervention represents an artificial treatment: dose, protocol, treatment
date, treatment start timestamp, duration, one supplier, and links to genes and
small molecules. Catalog entries are synthetic records, not biological reference
data. `treatment_started_at` is a UTC datetime feature generated from a date
between January 2024 and December 2025 and a seeded time of day. `created_at` is
the native database creation timestamp.

| Parameter | Default | Meaning |
| --- | --- | --- |
| `n_rows` | Required | Positive integer or strictly increasing nonempty list of positive integers; cumulative population targets |
| `n_repetitions` | `11` | Timed repetitions of every query at each milestone |
| `n_batch` | `20` | Number of UIDs in the batch lookup, capped by population |
| `random_seed` | `0` | Seed for synthetic values and reproducible UID sampling |
| `n_genes` | `100` | Gene catalog size; must be positive for the gene queries |
| `n_molecules` | `20` | Small-molecule catalog size |
| `genes_per_record_min`, `genes_per_record_max` | `3`, `5` | Inclusive number of gene links per intervention |
| `small_molecules_per_record_min`, `small_molecules_per_record_max` | `1`, `3` | Inclusive number of molecule links per intervention |
| `query_gene` | `gene_000` | Fixed target gene from the configured catalog |
| `query_protocol` | `protocol_B` | One of `protocol_A`, `protocol_B`, `protocol_C`, `protocol_D` |
| `query_treatment_start` | `"2025-01-01T00:00:00Z"` | Inclusive treatment-time lower bound |
| `query_treatment_end` | `"2025-07-01T00:00:00Z"` | Exclusive treatment-time upper bound |
| `existing_rows` | Omitted | Optional assertion of the detected starting population |

There are always 10 suppliers. Link bounds must satisfy
`0 <= min <= max <= catalog size`. Repetition and batch counts must be positive
integers; the seed must be nonnegative. Timestamp bounds must be quoted ISO 8601
strings with timezones and start must precede end. Bounds are normalized to UTC.
Invalid settings fail before tracking or population writes. All effective config
defaults become features of the experiment-run record.

## Resume

The experiment detects the existing population and publishes `starting_rows` in
`OUTPUT/run_info.yml`. The parent saves it on the run record and in
`Run.extra_data["run_info"]`, including when no milestone completes. There is no
startup observation.

Targets below the detected population are skipped. An equal target runs
maintenance and queries without inserting. Higher targets insert only the
remaining rows. If all targets are below the starting population, the run fails.
`existing_rows`, when supplied, must match the detected count; it does not
control how many rows are inserted.

Resume requires compatible catalog sizes and population workload metadata when
available. All interventions must have `treatment_started_at`; older populations
need a separate reset or backfill. Query filters may change between executions.
Historical insertion timings are not recovered, and individual historical
synthetic values are not exhaustively validated. RNG advancement continues from
the detected population outside insertion timing. Use one writer per load
instance; unexpected population changes detected at milestone boundaries stop
the execution.

## Insertion measurements

Transactions save up to 1,000 interventions at once; a milestone can end with a
smaller batch. Serialization failures, deadlocks and lock timeouts retry up to
three attempts, waiting one then two seconds. Other errors stop the experiment.

Each observation contains cumulative insertion timings **since this execution
started**, including earlier milestones. These snapshots overlap: use the
furthest completed snapshot from each execution when combining histories.
A resumed execution starts a new timing history at its detected population.

- `latency_ms` includes row generation, DataFrame construction, validation,
  saving, commits, failed attempts and retry waits.
- `save_ms` includes save attempts and commits, excluding row generation,
  DataFrame construction and retry waits.

Catalog/schema setup, RNG advancement, file serialization and PostgreSQL
`ANALYZE` are outside insertion timers. Times measure client operations, not
server-only SQL execution.

## Queries

| Operation / timing filename stem | Result |
| --- | --- |
| `uid_lookup` | One record by UID |
| `uid_with_features` | One record exported with all features |
| `uid_batch_with_features` | Up to `n_batch` records exported with all features |
| `recent_by_treatment_started_at` | Latest 100 records by treatment-time feature |
| `recent_by_created_at` | Latest 100 records by native creation time |
| `interventions_by_gene` | Latest 100 records linked to `query_gene`, by creation time |
| `interventions_by_gene_protocol_time` | Latest 100 records matching gene, protocol and treatment-time bounds, exported with all features |

Every operation is fully evaluated inside its timer and uses `n_repetitions`.
Targets, types, features and date bounds are resolved outside timing. UID samples
are reproducible and reused across repetitions at a milestone. UID breaks ties
for all timestamp orderings. Fewer than 100 matches, including zero, are valid.

The richer gene query is:

```python
gene_feature = ln.Feature.get(name="genes", is_type=False)
timestamp_feature = ln.Feature.get(name="treatment_started_at", is_type=False)

cohort = (
    ln.Record.filter(type=intervention, is_type=False)
    .filter(
        gene_feature == gene,
        protocol=query_protocol,
        treatment_started_at__gte=start,
        treatment_started_at__lt=end,
    )
    .order_by(-timestamp_feature, "-uid")
)
selected_uids = list(cohort.values_list("uid", flat=True)[:100])
result = (
    ln.Record.filter(uid__in=selected_uids)
    .order_by(-timestamp_feature, "-uid")
    .to_dataframe(include="features")
)
```

Explicit `gene_feature == gene` predicates distinguish our Record-valued feature
from Bionty's built-in `Record.genes` relation. UID selection caps the rich export
without passing a sliced RecordSet to the SDK exporter. Both selection and export
are timed together. Included features are the intervention's scalar annotations
and linked suppliers, genes and molecules.

First-call timings are kept separate from warm summaries; no cold-cache state
is implied. SQL execution counters and driver `execute()` timings are collected
inside the query timer. Fetching and Python processing remain part of total
latency but are outside the driver execute times.

Before each query round, an isolated root-role subprocess runs PostgreSQL
`ANALYZE`. The experiment retains its normal query role. Maintenance failure
prevents publication of that milestone. Diagnostic EXPLAIN replays happen after
timed repetitions and are outside their timers. Failed diagnostics produce a
warning and a report error; missing plans do not mean zero execution time.

## Observations and artifacts

`run.py` accepts two positional arguments, `CONFIG OUTPUT_DIRECTORY`. The parent
chooses the output folder. The child only writes files; the parent alone creates
tracking records and artifacts. See the [project README](../../readme.md) for the
complete handoff contract.

An observation is published by atomically renaming its completed temporary folder
to `OUTPUT/observations/record_scalability_<timestamp>_n_rows_<population>/`, for
example `record_scalability_2026-10-25T08:30:00Z_n_rows_10_000_000`.
The UTC timestamp marks completion, to second precision.

`manifest.yml` supplies observation features: scalar population, starting count,
rows inserted, workload and query settings, and `recorded_at`. The parent stores
`recorded_at` as a datetime column and puts the `observation` self-link first in
the sheet. No observation README is written. Measurement facts live in the
artifact's table columns, not artifact features.

| File | Contents |
| --- | --- |
| `insert_batches.parquet` | Cumulative per-batch timings, absolute populations before/after, attempts, retries, waits and success |
| `insert.parquet` | Cumulative insertion time and throughput for new records |
| Seven query `.parquet` files | Repetition, `latency_ms`, returned count (`value`), SQL counters, and applicable filters/limits |
| `explain.parquet` | One row per successfully collected logical query diagnostic: SQL, plan pairs, scan types, buffers, planning/execution times |
| `query_plans.html` | Browser-readable Python operations, resolved parameters, formatted SQL and collapsible execution plans |
| `health.parquet` | Record/event/auxiliary counts, connection/cache facts and insertion summaries |
| `table_distribution.parquet` | One row per database table: name, kind, row count, byte sizes and bloat estimate |

One Python operation can issue several SQL statements, particularly when
exporting features. The report shows one diagnostic replay per operation, with
all captured data SELECTs followed by their individual JSON plans. Authentication
and transaction-control statements are excluded. EXPLAIN execution-time totals
sum the available plans; they are distinct from end-to-end Python timings.
Fallback record-filter plans and unavailable diagnostics are labelled explicitly.

Observations retain creator provenance in `observation.run` and link to their
experiment-run record. They link only their own measurement files, without
tagging the entire run or transform. Published observations survive subsequent
failure. An interrupted milestone stays unpublished in `.pending`; a failed
local execution retains its output directory for inspection.

## Analysis

Analysis scripts and their shared helpers live in `analyses/`, separate from
population and measurement code. This is the recommended layout for experiments.

Four scripts analyze **one explicitly selected experiment-run record**. Pass the
record UID from `record_scalability / runs`, not the UID of its linked Lamin Run.
Each script follows the observations' `experiment_run` feature and loads only
directly linked measurement artifacts. It does not select other runs or require
`BENCHMARK_CONFIG`. Completed observations from an unfinished or failed execution
remain eligible. Older runs without linked observations are not inputs to these
new scripts; their stored historical analyses remain unchanged.

```sh
uv run python experiments/record_scalability/analyses/query_latency.py RUN_RECORD_UID
uv run python experiments/record_scalability/analyses/insert_batch_latency.py RUN_RECORD_UID
uv run python experiments/record_scalability/analyses/query_time_breakdown.py RUN_RECORD_UID
uv run python experiments/record_scalability/analyses/database_growth.py RUN_RECORD_UID
```

All four expose `analyze(run_record)` and `analyze(run_record_uid)` for Python:

```python
from experiments.record_scalability.analyses.query_latency import analyze

analyze(run_record)
```
An existing Record object must belong to the configured tracking instance. The
scripts connect and start tracking only when `analyze()` or the CLI is invoked;
importing them does not run an analysis.

Analysis transforms use the key
`benchmarks/experiments/record_scalability/<source_run_uid>/<analysis_name>`, where
`source_run_uid` is the source benchmark's Lamin Run UID. Each source execution
and analysis has its own transform version family. Repeating the same analysis
for the same source execution reuses that family and creates a new analysis run.

### Query latency

`query_latency.py` reads the query timings from every completed observation of
the selected execution. Its summary contains one row per query and `n_rows`:

- Sample count, minimum, maximum, mean, sample standard deviation (`ddof=1`),
  median, Q1/Q3, IQR, and p01, p05, p90, p95 and p99.
- Tukey fences (`Q1 - 1.5 IQR`, `Q3 + 1.5 IQR`), actual adjacent-value whiskers,
  outlier count and outlier values, sufficient to recreate the boxplot.
- First-call latency, separate `warm_*` statistics for repetitions after the
  first, plus the original repetition and latency lists.
- Source run-record, Lamin Run, observation and artifact UIDs, and the
  observation completion timestamp.

All repetitions, including the first call, contribute to the plotted boxes.
Each query has its own facet, with independent x and y axes: population is a
categorical x-axis and latency in milliseconds is the y-axis. Whiskers and
outliers are plotted from the saved summary. Green triangles mark the mean of
all calls; the dark line inside each box marks the median. A legend labels both.
Quantiles use linear interpolation. Standard deviation is absent for one sample;
warm statistics are absent when only the first call exists. With 11 repetitions,
high percentiles describe the observed calls rather than a reliable tail estimate.

### Batch insertion latency

`insert_batch_latency.py` selects only the observation with the highest `n_rows`.
Its `insert_batches.parquet` already contains the entire insertion history of
this execution, so earlier snapshots are not loaded or concatenated. Different
batch sizes, including partial milestone-ending batches, are retained.

The plot shows batch latency in milliseconds against intervention population
immediately after each batch (`records_after`). Batch latency includes generation,
saving, commits and retry waits; queries, maintenance and pauses between milestones
are excluded. The output table preserves original batch columns and adds source
provenance.

Duplicate observations at the same population, invalid/nonfinite timings,
incomplete query repetitions and inconsistent insertion histories fail explicitly.
A query-only run with no inserted batches has no insertion plot.

### Database calls versus remaining query time

`query_time_breakdown.py` reads the same query repetitions as `query_latency.py`.
For each repetition it splits `latency_ms` into the measured `sql_execute_ms`
and the remaining time. Each query has two panels: stacked mean durations by
population, and the component percentages. All calls are included. Means are
stacked because they add up to mean total latency; component medians do not.
Percentages divide summed component durations by summed total latency.

The summary has one row per query/population, including component means,
medians, p95s, fractions, SQL statement counts, first/warm-call statistics,
per-repetition values and source provenance. Missing driver timings and invalid
decompositions fail explicitly. Sub-millisecond rounding discrepancies up to
0.002 ms are clamped and counted; zero-duration groups have undefined fractions.

Database-call time includes driver execution and network waits; it is not pure
PostgreSQL execution time. The remainder includes fetching, Python processing
and instrumentation overhead outside those calls; it is not CPU time. EXPLAIN
timings come from separate diagnostic replays and are not subtracted here.

### Database and table growth

`database_growth.py` reads `table_distribution.parquet` at every milestone and
uses `health.parquet` for total database bytes when available. The overview shows:

- Whole-database storage versus the sum of captured table storage.
- The 12 largest captured tables at the last milestone, split into data, indexes
  and other/TOAST storage.
- Rows and storage per intervention for up to eight of the largest tables.

The main parquet table contains one row per table and population, original
counts and byte sizes, changes since the preceding measured snapshot, rows and
bytes per intervention, bytes per table row, index fraction and provenance.
`table_review.parquet` summarizes each table's latest size, added rows/bytes,
coverage and row/storage growth relative to population growth. The review is
sorted by added bytes, then latest size, to help choose tables for investigation.
A normalized growth factor above 1 means faster growth than population, not
proof of a performance defect. Zero baselines and single-snapshot growth rates
are left undefined.

Open `tables.html` in the artifact browser for the overview and review table,
then expand any table to see four individual plots and its exact measurements.
Every captured table gets a drill-down, including ones outside the overview's
top-table selections. Missing snapshots break lines rather than becoming zeros.
Each individual row-count plot includes a least-squares line
`table_rows = k * n_rows + intercept`, with `k` labelled in table rows per
intervention. The intercept is fitted rather than forced to zero, allowing for
pre-existing rows. Only measured snapshots enter the fit. With fewer than two
distinct populations, the slope is unavailable. The review table also saves
`row_slope_k` and `row_fit_intercept`.

Captured distributions cover Record-related tables and the write-event table,
not the whole database. Total database storage may therefore be larger than the
sum of captured tables. Data/index totals also exclude some TOAST/overhead bytes
that are included in total relation storage. The stored `bloat_ratio` is a
PostgreSQL dead/live-row estimate, labelled as such in the report; it is not a
measurement of physical bloat. Missing table distributions fail explicitly;
missing database-size health values remain absent.

### Saved results

Each script creates a tracked analysis linked to its source record and input
artifacts, saving a parquet table and SVG/PNG figures in the benchmark instance.
Keys are scoped to the source record:
`benchmarks/analysis/record_scalability/<run_record_uid>/<analysis_name>.*`.
The database-growth analysis additionally saves its HTML drill-down report and
per-table review under that key's folder. No analysis resets or writes to the
load instance.
