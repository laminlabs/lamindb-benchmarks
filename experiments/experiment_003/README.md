---
description: Page-sort cost for a date stored in RecordJson
---

# Ordering a sheet by a date in RecordJson

Measures the SQL a paged sheet would run to order records by a date stored in `RecordJson`. `to_dataframe()` is not timed. It loads the whole type and can sort in pandas.

`Samples` is one record type. `collected_on` is one `date` feature. Each record has one `RecordJson` row for that feature, except about 10% where the date is missing. The page is therefore a left join, and the sort has nulls. Dates are shuffled from 2010-01-01 through 2024-12-31, so insertion order is not chronological. No extra index or table is created.

The page query casts the stored JSON string to `date`. Ordering `j.value` itself is jsonb order, not date order.

```sql
SELECT r.id
FROM lamindb_record r
LEFT JOIN lamindb_recordjson j
  ON j.record_id = r.id AND j.feature_id = :collected_on_id
WHERE r.type_id = :samples_id
ORDER BY (j.value #>> '{}')::date
LIMIT 50;
```

One `EXPLAIN (ANALYZE, BUFFERS)` records whether that sort spilled to disk. It is not part of the latency sample.

## Instance setup

The database under test is the load instance, `laminlabs/lamindb-load-test`. A batch wipes it before every config, so each size starts empty. The wipe truncates user tables and write events. It keeps bionty, migrations, users, storage, runs, and transforms. It does not drop indexes. This experiment creates no index and no extra table, so a later experiment is not left with one.

`setup.py` then builds the sheet on that empty instance:

- One record type, `Samples`.
- One feature, `collected_on`, dtype `date`, and a schema on the type that contains it.
- `n_rows` records, inserted with `bulk_create`.
- One `RecordJson` row per record for `collected_on`, except about 10% with the date left missing. Dates are drawn from 2010-01-01 through 2024-12-31 and shuffled, so insertion order is not chronological.

The page query runs on that same instance, with RLS enabled and the normal schema and indexes. The benchmark instance, `laminlabs/lamindb-benchmarks`, only stores the run and the measurements. It is not the database being sorted.

## Measurements

The runner uploads these under `benchmarks/measurements/experiment_003/<run uid>/`.

- `populate.parquet`: records created, JSON rows, and how many dates were left missing.
- `page_date.parquet`: one latency per repetition. `value` is the number of rows returned.
- `explain_page_date.parquet`: the explain plan. `value` is 1 when the sort spilled to disk.

## Analysis

A vertical box per record count. The x axis is categorical. Every repetition is included, including the cold first call.

```
python experiments/experiment_003/analyze.py
```

- `benchmarks/analysis/experiment_003/page_sort.parquet`: one row per timed repetition, plus whether that size spilled.
- `benchmarks/analysis/experiment_003/page_sort.svg`: the boxplot.

## Parameters

- `n_rows`: How many Samples to create before timing the page. Integer, at least 1. The ladder is 100, 1,000, and 10,000.
- `n_repetitions`: How many times to time the page. Integer, at least 1. Defaults to 11 when omitted. The first call is cold and is included.
