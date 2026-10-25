---
description: Cost of enforcing ToOne links on RecordRecord
---

# Cost of enforcing ToOne links

Measures the insert cost of a partial unique index that allows one `to_one` link per record and feature. The comparison is the same multi-row insert before and after that index. Both arms see the new column and an empty link table. Each timed repetition is rolled back, so the table stays empty and the repetitions stay comparable.

`Subjects` is the record type under test. `Sites` is a small catalog. `site` is one record-valued feature. Setup creates the records and the feature. It does not insert links and does not add the column. The query script inserts the links with raw SQL and `to_one` true. An ORM insert would leave the column at its default `false`, and the partial index would not be exercised.

The column add is timed separately. On Postgres 11 or newer a constant default is often metadata-only, so the byte change on an empty link table can be 0. After the timed repetitions, one batch is committed only to measure the index with rows in it, then those links are deleted. That batch is not part of the latency sample.

## Execution and observations

The single entry point is `run.py CONFIG OUTPUT_DIRECTORY`. Population and
query logic live in helper modules, called by that one script. On success it
publishes `OUTPUT_DIRECTORY/observations/exp_4_n_rows_<population>/` by
atomically renaming a temporary directory. Only the parent runner uploads the
files and creates the observation record. Measurement metadata is stored in
the parquet columns; the observation links these artifacts to the tracked
execution. Earlier historical measurements linked directly to run records
remain supported by the analysis scripts.

## Schema

The load instance is Postgres. RLS stays enabled for the inserts. The column and index are added with the root database user, because the load user cannot alter the table and a Django transaction can roll the DDL back. The root connection is autocommit. After each DDL call the process reconnects as the load user.

A wipe truncates rows. It does not drop this column or index. Both are dropped at the start of setup and query, and again before query exits, including on failure. Committed links for `site` are deleted too. The exit fails if either object is still present.

```sql
ALTER TABLE lamindb_recordrecord
  ADD COLUMN to_one boolean NOT NULL DEFAULT false;
```

```sql
CREATE UNIQUE INDEX unique_recordrecord_to_one
  ON lamindb_recordrecord (record_id, feature_id)
  WHERE to_one;
```

The insert, one row of the multi-row statement:

```sql
INSERT INTO lamindb_recordrecord (record_id, feature_id, value_id, to_one)
VALUES (:subject_id, :site_feature_id, :site_id, true);
```

## Measurements

The runner uploads these under `benchmarks/measurements/experiment_004/<run uid>/`.

- `populate.parquet`: subjects and sites created. No links yet. `value` is the subject count.
- `insert_links.parquet`: one latency per repetition before the index. `value` is the inserted row count. The repetition is rolled back.
- `insert_links_indexed.parquet`: the same insert after the index. `value` is the inserted row count.
- `relation_size.parquet`: one row. `value` is the total-byte change from adding the column. Features hold the empty index size, the populated index size, and the DDL times.

## Analysis

Latest succeeded run at each subject count. The first insert is the cold call and is included.

```
python experiments/experiment_004/analyze.py
```

- `benchmarks/analysis/experiment_004/insert_latency.parquet`: one row per timed insert repetition.
- `benchmarks/analysis/experiment_004/insert_latency.svg`: grouped boxes, one pair per subject count.
- `benchmarks/analysis/experiment_004/relation_size.parquet`: column bytes, index bytes, and DDL times.
- `benchmarks/analysis/experiment_004/relation_size.svg`: bars for those byte counts and DDL times.

## Parameters

- `n_rows`: How many Subjects to create, and how many links each timed insert writes. Integer, at least 1. The current ladder is 100 and 1,000.
- `n_values`: How many Sites to create. Links round-robin through them. Integer, at least 1. Defaults to 100 when omitted. Set it in every config so the run sheet stays stable.
- `n_repetitions`: How many times to time each insert. Integer, at least 1. Defaults to 11 when omitted. The first call is cold and is included.
