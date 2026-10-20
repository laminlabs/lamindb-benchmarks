---
description: Intervention lookup and database growth as records are added
---

# Intervention lookup and database growth as records are added

Measures how long it takes to fetch intervention records by uid, with and without their features, after a controlled number of them have been created. It also records how that growth shows up in the database: write events, auxiliary tables, disk, insert rate, and whether each lookup plan used an index or a sequential scan.

Each intervention is one treatment: a dose, a protocol, a treatment date and duration, one supplier, and links to a few genes and small molecules. Catalog sizes stay fixed within each run. By default an intervention links 3 to 5 of 100 genes and 1 to 3 of 20 molecules, and its supplier is one of 10. Gene/molecule catalog sizes and per-record link ranges are configurable independently.

Records and their features are saved in batches of 1,000, with each batch in a transaction. Serialization failures, deadlocks, and lock timeouts are retried up to three attempts, waiting 1 then 2 seconds after rollback. Other errors, including validation and connection errors, stop the experiment immediately. Exhausted retries also stop the experiment; there is no per-record fallback. Retry time is included in the insert measurement.

The first repetition of each lookup is labeled the first call; this does not imply a cold database cache, since population and sampling have already accessed the database. Later repetitions use the same uids. The value recorded for a timed call is the number of rows that call returned. The insert is timed as well. Write events, auxiliary-table entries, disk space, cache hit ratio, and one EXPLAIN ANALYZE per lookup are captured during the run, because the load database is wiped between configs. A plan counts as an index scan only when every explained record or link statement used an index and none used a sequential scan.

Population and sampling use separate random generators with the same configured seed. Lookup candidates are ordered by name and uid before sampling. Each lookup artifact records the effective seed, catalog sizes, link ranges, and the exact sampled UIDs (as a JSON string in `sampled_uids`). A rerun generates new record UIDs but reproduces the synthetic feature values and selection positions for the same workload and seed.

## Queries

Three lookups are timed on the same sampled records.

`uid_lookup` fetches one intervention by uid and does not load its features.

```python
ln.Record.filter(uid=uid).one_or_none()
```

`uid_with_features` fetches that same record and includes the feature values.

```python
ln.Record.filter(uid=uid).to_dataframe(include="features")
```

`uid_batch_with_features` fetches up to `n_batch` interventions, including their features. If fewer records exist, it uses all of them.

```python
ln.Record.filter(uid__in=uids).to_dataframe(include="features")
```

## Measurements

Each fact is one parquet. The runner uploads it under `benchmarks/measurements/experiment_002/<run uid>/`.

- `insert.parquet`: time to create the intervention records, the insert rate, and the write events produced by that insert.
- `insert_batches.parquet`: one row per attempted batch, including batch size, cumulative successful records before/after, total batch latency, save-attempt time, retry wait time, attempt/retry counts, success status, and the last attempt's SQLSTATE. Written even if a batch fails. Total insert time is the sum of batch latencies (including row generation and retry waits), excluding serialization of these diagnostics. Overall retry and batch counts are also saved on `insert.parquet`.
- `uid_lookup.parquet`: latency of each repetition of the uid-only lookup, and how many rows it returned.
- `uid_with_features.parquet`: the same, for the single-record lookup that includes features.
- `uid_batch_with_features.parquet`: the same, for the batch lookup that includes features.
- `explain_uid_lookup.parquet`, `explain_uid_with_features.parquet`, `explain_uid_batch_with_features.parquet`: one replay after the timed calls. The parquet body includes `sql` (one captured lookup statement with its parameter values) and `explain_analyze` (the full JSON EXPLAIN ANALYZE output for all explained statements in that replay). Metadata holds the scan type, planning time, execution time, and a truncated plan. If capture fails and only the record-filter fallback is explained, `sql` is empty and `plan_source` is `record_filter`. Insert SQL is not captured.
- `health.parquet`: write events, auxiliary-table entries, disk space, cache hit ratio, and active connections after the run.
- `table_<name>.parquet`: row count, data size, index size, and bloat for one record-related table, or for the write-event table.

Each timed lookup row also contains `sql_execute_count` and `sql_execute_ms`. These count Django cursor executions and time their execute calls, including network waits; they exclude fetching and Python/DataFrame processing. One cursor execution may contain multiple SQL statements, including an RLS token call. Lightweight counters run inside the latency timer, so their overhead is included. SQL text capture and EXPLAIN remain outside the timer.

EXPLAIN uses `ANALYZE, BUFFERS, FORMAT JSON`. Its metadata includes shared/local hit, read, dirtied, and written blocks, plus temporary read/written blocks. These sum execution-plan root counters across statements without double-counting child nodes. Full buffer detail, including planning buffers where provided, remains in the JSON. These are measurements of the later EXPLAIN replay, not the timed repetitions.

## Analysis

The analysis scripts compare one catalog-size/link-range workload at a time. By default they select 100 genes, 20 molecules, and the original 3–5 gene and 1–3 molecule ranges. Set `BENCHMARK_CONFIG` to a variant's config when analyzing that variant; other catalog sizes or link ranges are excluded so they cannot be silently pooled or replace one another at the same record count. Seeds may differ across included runs. Newly generated summary tables use `latency_first_call_ms` and `first_call` instead of the previous cold-cache labels. Repetition counts and warm-summary calculations are unchanged.

Warm median latency. Error bars span the warm 5th to 95th percentile:

```
python experiments/experiment_002/analyze.py
```

- `benchmarks/analysis/experiment_002/latency_median_p95.parquet`: one row per lookup and record count, with the first call, warm median, and warm 5th and 95th percentiles.
- `benchmarks/analysis/experiment_002/latency_median_p95.svg`: unconnected scatter of that median. Error bars span the 5th to the 95th percentile.

Eight-panel dashboard of the latest succeeded run at each size:

```
python experiments/experiment_002/analyze_dashboard.py
```

- `benchmarks/analysis/experiment_002/dashboard_snapshot.parquet`: write events, auxiliary entries, disk, cache hit ratio, and insert rate at each record count.
- `benchmarks/analysis/experiment_002/dashboard_queries.parquet`: warm latency, database execution time, and scan type for each lookup.
- `benchmarks/analysis/experiment_002/dashboard.svg`: the eight panels as unconnected markers. The latency panel uses the same error bars as the latency figure.

Vertical boxplots of every timed repetition, one facet per query, with a shared categorical x axis:

```
python experiments/experiment_002/analyze_latency.py
```

- `benchmarks/analysis/experiment_002/latency_boxplot.parquet`: one row per timed repetition for the latest succeeded run at each record count.
- `benchmarks/analysis/experiment_002/latency_boxplot.svg`: three vertical boxplot facets. The x axis categories are the record counts.

Setup time, total rows, and database size as bar charts over those same categories:

```
python experiments/experiment_002/analyze_setup.py
```

- `benchmarks/analysis/experiment_002/setup_summary.parquet`: one row per record count, with setup time, insert rate, total rows, disk bytes, write events, and auxiliary entries.
- `benchmarks/analysis/experiment_002/setup_summary.svg`: one bar-chart facet per fact, with a shared categorical x axis.

## Parameters

- `n_rows`: How many intervention records to create before timing the lookups. Integer, at least 1.
- `n_repetitions`: How many times to time each lookup. Integer, at least 1. Defaults to 3 when omitted.
- `n_batch`: How many records the batch lookup fetches. Integer, at least 1. Defaults to 20 when omitted. If fewer records exist, the lookup uses all of them.
- `random_seed`: Nonnegative integer used for population and lookup sampling. Defaults to 0.
- `n_genes`: Total number of distinct genes in the catalog. Nonnegative integer; defaults to 100.
- `n_molecules`: Total number of distinct small molecules in the catalog. Nonnegative integer; defaults to 20.
- `genes_per_record_min`, `genes_per_record_max`: Inclusive link-count range, default 3 and 5. Require `0 <= min <= max <= n_genes`.
- `small_molecules_per_record_min`, `small_molecules_per_record_max`: Inclusive link-count range, default 1 and 3. Require `0 <= min <= max <= n_molecules`.

Link-range defaults do not shrink automatically with the catalog. If a catalog is smaller than its default maximum, configure the link range too. An empty catalog requires both corresponding link bounds to be 0.

For a denser workload, keep the same record counts and set, for example:

```yaml
random_seed: 42
n_genes: 500
n_molecules: 50
genes_per_record_min: 20
genes_per_record_max: 20
small_molecules_per_record_min: 10
small_molecules_per_record_max: 10
```
