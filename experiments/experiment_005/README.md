---
description: Record scalability’s intervention workload via the Notion REST API, tracked in LaminDB
---

# Notion intervention population and lookup benchmark

`experiment_005` repeats record scalability’s synthetic workload against Notion. It
creates one page per intervention, with the same dose, protocol, treatment date,
duration, supplier, and gene/molecule link ranges. The default catalogs contain
10 suppliers, 100 genes and 20 small molecules. Each intervention links one
supplier, 3–5 genes and 1–3 molecules. The seed and random-call order match
record scalability. All effective defaults are tracked on the Lamin run and run sheet.

There are four Notion databases/data sources: `suppliers`, `genes`,
`small_molecules`, and `interventions`, under a dedicated dataset page. Each new
run creates its own dataset unless explicitly given an existing dataset ID.
Nothing is reset, deleted, or written to the Lamin load-test instance.

## Execution and observations

The single entry point is `run.py CONFIG OUTPUT_DIRECTORY`. Population and
query logic live in helper modules, called by that one script. On success it
publishes `OUTPUT_DIRECTORY/observations/exp_5_n_rows_<population>/` by
atomically renaming a temporary directory. Only the parent runner uploads the
files and creates the observation record. Measurement metadata is stored in
the parquet columns; the observation links these artifacts to the tracked
execution. Earlier historical measurements linked directly to run records
remain supported by the analysis scripts.

## Credentials and target

`NOTION_API_KEY` is an environment variable, not a token type. It accepts either
a Notion PAT (your user's permissions) or an internal connection token (explicit
page access for that bot). Grant read/insert/update access to a dedicated parent
page. Set `NOTION_PARENT_PAGE_ID` to its bare UUID. Do not put tokens in YAML:
the config is tracked in LaminDB. The API version is pinned to `2026-03-11`.

The client uses `httpx` against Notion's documented REST API so all request
attempts, pacing and retries can be measured explicitly, without SDK retries.

## Run locally

Install the locked dependencies with `uv sync`. Supply `LAMIN_API_KEY` (or your
existing Lamin login), `NOTION_API_KEY`, and `NOTION_PARENT_PAGE_ID` in the process
environment, then run:

```sh
LAMIN_CURRENT_INSTANCE=laminlabs/lamindb-benchmarks \
  uv run python benchmarks/run_benchmark.py experiments/experiment_005/example.yml
```

The runner resolves defaults, starts an independent experiment_005 Lamin run,
executes the single experiment process, and uploads its completed observation
under `benchmarks/measurements/experiment_005/<run uid>/observations/`.
The observation links the measurements and experiment-run record in
`laminlabs/lamindb-benchmarks`. The child uses Notion as its load target and
does not connect to the Lamin load instance.

## Run on Modal

In workspace `alex-76941`, environment `Benchmarks`, keep the existing
`lamin-benchmarks` secret and create `notion-benchmarks` containing `NOTION_API_KEY`
and `NOTION_PARENT_PAGE_ID`:

```sh
modal run --detach --profile alex-76941 --env Benchmarks benchmarks/modal_benchmarks.py \
  --config experiments/experiment_005/example.yml --region eu-west \
  --notion-secret notion-benchmarks
```

The additional secret is mounted only when `--notion-secret NAME` is supplied.
Use the exact secret name in your Modal workspace. Modal runs the client in the selected region; it cannot
pin Notion's own service region. This does not run the analyses automatically.

## Resume and grow

Setup prints the dataset page URL immediately and saves all database/data-source
IDs in `target.parquet`. To add 100 records to a dataset containing exactly 100:

```yaml
name: "Notion: grow from 100 to 200 interventions"
experiment: experiments/experiment_005
n_rows: 200
existing_rows: 100
notion_dataset_page_id: "<the dataset page UUID, not the parent page>"
n_repetitions: 11
n_batch: 20
```

Alternatively set `NOTION_DATASET_PAGE_ID` in the environment. Catalog contents,
relation targets, property types and exact indexed intervention population must
match before inserting. Every benchmark UID is deterministic. An interrupted
group can leave individual pages committed, so determine the exact existing
population before resuming. Do not run two writers against the same dataset.
An ambiguous creation error stops the run, rather than blindly creating a second
page. A new top-level setup page/database can also remain after an ambiguous
write; inspect the parent page before retrying a fresh run.

## Timings and API limits

Notion creates pages individually; a group of 1,000 is a reporting interval, not
an atomic bulk insert. `insert_batch_size` defaults to 1,000 independently of
the lookup `n_batch` (20). Each completed or failed group checkpoints timing and
state in the runner's shared results directory and prints progress. A failed
group records the confirmed successful pages and stops. The runner uploads
partial measurements on an ordinary exception. Abrupt container termination
can still lose local diagnostics; this is not persistent remote checkpointing.

`latency_ms` is total client elapsed time, including generation, HTTP, pacing
and retries. `api_request_ms` is HTTP elapsed time including network and response
transfer, not server-only execution. `rate_limit_wait_ms` records waits separately.
`insert.parquet` covers only this run's newly inserted pages. Catalog creation,
resume inventory, RNG advancement and checkpoint serialization are outside that
insertion timer. Query timers include response decoding, relation-name mapping
from the catalog cache, and DataFrame construction for feature queries.

Default pacing is 3 requests/second, configurable using
`notion_requests_per_second`. Respect the workspace plan and shared budgets.
429/529 responses honor `Retry-After` with bounded retries; a blocked connection
fails immediately. Read-only requests also retry transport failures and
500/502/503/504. Creation requests do not retry ambiguous transport/5xx failures.
`notion_max_retries` defaults to 5. Large populations can exceed the Modal
launcher's 24-hour limit; choose bounded increments. This is API ingestion
throughput, not a comparison of database bulk-insert engines.

## Lookups

- `uid_lookup`: filter by the synthetic `benchmark_uid`, projecting that property.
- `uid_with_features`: the same filter returning all properties and relation IDs,
  mapped to names from the already-loaded catalogs and converted to a DataFrame.
- `uid_batch_with_features`: OR-filter up to `n_batch` sampled UIDs, returning
  full properties. This includes any API pagination, not 20 direct page GETs.

All use the same sampled UIDs for 11 repetitions; first call is reported
separately from ten subsequent calls. This does not establish a cold server cache.
A readiness probe outside the timer waits briefly for the last new page to be
queryable. Query result IDs/counts are checked. Relation lists must contain at
most 25 entries per property; larger workloads fail validation instead of
benchmarking incomplete data. `n_batch` must be <=100.

Exact population inventory is partitioned into numeric index ranges of <=1,000
records, avoiding Notion's 10,000-result query cap. Incomplete pagination is an
error. Full inventory and catalog verification happen outside query timers.

## Measurement files

- `target.parquet`: dataset/database/data-source IDs, with no credentials.
- `insert.parquet`: this run's insertion total, throughput, retry and batch counts.
- `insert_batches.parquet`: per-group elapsed time, confirmed count, absolute
  records before/after, success, HTTP attempts/time/bytes, waits and retries.
- `uid_lookup.parquet`, `uid_with_features.parquet`,
  `uid_batch_with_features.parquet`: repetitions and HTTP counters/elapsed time.
- `setup_requests.parquet`, `query_requests.parquet`: one row per HTTP attempt,
  with operation, batch/repetition, endpoint, status, elapsed time, response bytes,
  wait, attempt number and response request ID. No authorization headers or bodies.
- `health.parquet`: exact page/catalog/relation counts, insertion facts, and an
  explicit unavailable marker for server metrics.
- `table_suppliers.parquet`, `table_genes.parquet`, `table_small_molecules.parquet`,
  `table_interventions.parquet`: logical page counts. Physical byte sizes are null.
- `table_links_supplier.parquet`, `table_links_genes.parquet`,
  `table_links_small_molecules.parquet`: relation reference counts.
- `explain_<lookup>.parquet`: `supported=false` and null timings/values. Notion
  exposes no SQL EXPLAIN, server execution times, scan types, cache hit rates,
  physical disk/index sizes or database audit log. These files are explicit
  unavailable markers, never synthetic measurements. There is no `ANALYZE` step.

## Analyses

Run each through the same local environment with
`LAMIN_CURRENT_INSTANCE=laminlabs/lamindb-benchmarks`:

```sh
uv run python experiments/experiment_005/analyze.py
uv run python experiments/experiment_005/analyze_latency.py
uv run python experiments/experiment_005/analyze_setup.py
uv run python experiments/experiment_005/analyze_dashboard.py
```

Each saves tables and SVGs plus an analysis-sheet row in the benchmark instance.
Keys under `benchmarks/analysis/experiment_005/` mirror record scalability:
`latency_median_p95`, `latency_boxplot`, `setup_summary`, and `dashboard` (the
latter has `dashboard_snapshot` and `dashboard_queries` tables). The dashboard
uses Notion-available page/link counts, insert time/throughput, warm client/HTTP
latency, pacing/retry waits and response bytes. Latest successful run per size;
match seed, catalogs, link ranges, API version, pacing and lookup batch size.
Set `BENCHMARK_CONFIG` to select a different workload. Different workspace plans
and service regions should still be interpreted separately.

References:
- https://developers.notion.com/reference/create-database
- https://developers.notion.com/reference/property-object
- https://developers.notion.com/reference/query-a-data-source
- https://developers.notion.com/reference/request-limits
- https://developers.notion.com/guides/get-started/personal-access-tokens
