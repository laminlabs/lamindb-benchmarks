---
description: Count latency as the number of records grows
---

# Count latency as the number of records grows

Measures how long a count takes after a controlled number of records have been created. The count selects records whose names share this experiment's prefix, so the timed query stays limited to those rows as the registry grows. Each repetition records its latency.

## Parameters

- `n_rows`: How many records to create before timing the count. Integer, at least 0.
- `n_repetitions`: How many times to time the count. Integer, at least 1. Defaults to 3 when omitted.
