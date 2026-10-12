---
description: Intervention lookup and database growth as records are added
---

# Intervention lookup and database growth as records are added

Measures how long it takes to fetch intervention records by uid, with and without their features, after a controlled number of them have been created. It also records how that growth shows up in the database: write events, auxiliary tables, disk, insert rate, and whether each lookup plan used an index or a sequential scan.

Each intervention is one treatment: a dose, a protocol, a treatment date and duration, one supplier, and links to a few genes and small molecules. The supplier, gene, and molecule catalogs stay fixed, so growth comes from the interventions, not from a larger catalog. An intervention links 3 to 5 of 100 genes and 1 to 3 of 20 molecules, and its supplier is one of 10.

The first repetition of each lookup is the cold call. Later repetitions use the same uids. The value recorded for a timed call is the number of rows that call returned. The insert is timed as well. Write events, auxiliary-table entries, disk space, cache hit ratio, and one EXPLAIN ANALYZE per lookup are captured during the run, because the load database is wiped between configs. A plan counts as an index scan only when every explained record or link statement used an index and none used a sequential scan.

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
- `uid_lookup.parquet`: latency of each repetition of the uid-only lookup, and how many rows it returned.
- `uid_with_features.parquet`: the same, for the single-record lookup that includes features.
- `uid_batch_with_features.parquet`: the same, for the batch lookup that includes features.
- `explain_uid_lookup.parquet`, `explain_uid_with_features.parquet`, `explain_uid_batch_with_features.parquet`: one EXPLAIN ANALYZE after the timed calls. Holds the scan type, planning time, execution time, and a truncated plan.
- `health.parquet`: write events, auxiliary-table entries, disk space, cache hit ratio, and active connections after the run.
- `table_<name>.parquet`: row count, data size, index size, and bloat for one record-related table, or for the write-event table.

## Analysis

Warm median latency. Error bars span the warm 5th to 95th percentile:

```
python experiments/experiment_002/analyze.py
```

- `benchmarks/analysis/experiment_002/latency_median_p95.parquet`: one row per lookup and record count, with the cold call, warm median, and warm 5th and 95th percentiles.
- `benchmarks/analysis/experiment_002/latency_median_p95.svg`: unconnected scatter of that median. Error bars span the 5th to the 95th percentile.

Eight-panel dashboard of the latest succeeded run at each size:

```
python experiments/experiment_002/analyze_dashboard.py
```

- `benchmarks/analysis/experiment_002/dashboard_snapshot.parquet`: write events, auxiliary entries, disk, cache hit ratio, and insert rate at each record count.
- `benchmarks/analysis/experiment_002/dashboard_queries.parquet`: warm latency, database execution time, and scan type for each lookup.
- `benchmarks/analysis/experiment_002/dashboard.svg`: the eight panels as unconnected markers. The latency panel uses the same error bars as the latency figure.

## Parameters

- `n_rows`: How many intervention records to create before timing the lookups. Integer, at least 1.
- `n_repetitions`: How many times to time each lookup. Integer, at least 1. Defaults to 3 when omitted.
- `n_batch`: How many records the batch lookup fetches. Integer, at least 1. Defaults to 20 when omitted. If fewer records exist, the lookup uses all of them.
