---
description: Intervention lookup and database growth as records are added
---

# Intervention lookup and database growth as records are added

Measures how long it takes to fetch intervention records by uid, with and without their features, after a controlled number of them have been created. It also records how that growth shows up in the database: write events, auxiliary tables, disk, insert rate, and whether each lookup plan used an index or a sequential scan.

Each intervention is one treatment: a dose, a protocol, a treatment date and duration, one supplier, and links to a few genes and small molecules. The supplier, gene, and molecule catalogs stay fixed, so growth comes from the interventions, not from a larger catalog. An intervention links 3 to 5 of 100 genes and 1 to 3 of 20 molecules, and its supplier is one of 10.

Three lookups are timed on the same sampled records. One fetches a single record by uid. One fetches that record together with its features. One fetches a batch of records together with their features. Each repetition of a lookup uses the same uids. The first repetition is the cold call. The value recorded for a timed call is the number of rows that call returned.

The run also times the insert. After the lookups it records write events, auxiliary-table entries, disk space, cache hit ratio, and one EXPLAIN ANALYZE per lookup. Those facts are captured during the run because the load database is wiped between configs. A plan counts as an index scan only when every explained record or link statement used an index and none used a sequential scan.

## Analysis

Warm median and 95th percentile latency:

```
python experiments/experiment_002/analyze.py
```

Eight-panel dashboard, including index versus sequential scan:

```
python experiments/experiment_002/analyze_dashboard.py
```

## Parameters

- `n_rows`: How many intervention records to create before timing the lookups. Integer, at least 1.
- `n_repetitions`: How many times to time each lookup. Integer, at least 1. Defaults to 3 when omitted.
- `n_batch`: How many records the batch lookup fetches. Integer, at least 1. Defaults to 20 when omitted. If fewer records exist, the lookup uses all of them.
