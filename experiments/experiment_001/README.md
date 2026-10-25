---
description: Count latency as the number of records grows
---

# Count latency as the number of records grows

Measures how long a count takes after a controlled number of records have been created. The count selects records whose names share this experiment's prefix, so the timed query stays limited to those rows as the registry grows. Each repetition records its latency.

## Execution and observations

The single entry point is `run.py CONFIG OUTPUT_DIRECTORY`. Population and
query logic live in helper modules, called by that one script. On success it
publishes `OUTPUT_DIRECTORY/observations/exp_1_n_rows_<population>/` by
atomically renaming a temporary directory. Only the parent runner uploads the
files and creates the observation record. Measurement metadata is stored in
the parquet columns; the observation links these artifacts to the tracked
execution. Earlier historical measurements linked directly to run records
remain supported by the analysis scripts.

## Parameters

- `n_rows`: How many records to create before timing the count. Integer, at least 0.
- `n_repetitions`: How many times to time the count. Integer, at least 1. Defaults to 3 when omitted.
