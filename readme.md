# LaminDB benchmarks

One config starts one tracked execution. Each experiment has one entry point,
`run.py CONFIG OUTPUT_DIRECTORY`, and controls its own sequence of writes,
queries, and maintenance. The parent runner uploads observations while the
experiment is still running. There is no batch runner and no automatic reset.

## Run an experiment

Run commands from the repository root using the project environment:

```sh
uv run python benchmarks/run_benchmark.py experiments/record_scalability/example.yml
```

Or run the same config on Modal:

```sh
modal run --detach --profile alex-76941 --env Benchmarks benchmarks/modal_benchmarks.py \
  --config experiments/record_scalability/example.yml --region eu-west
```

The tracking instance (`LAMIN_CURRENT_INSTANCE`, default
`laminlabs/lamindb-benchmarks`) stores runs, observations, measurement artifacts,
and analyses. The load instance (`LAMIN_LOAD_INSTANCE`, default
`laminlabs/lamindb-load-test`) receives the operations under test. They must be
different. The child process connects only to the load target; the parent keeps
its connection to the tracking instance. Only the parent uploads tracking
records and artifacts.

## Parent and child contract

`benchmarks/run_benchmark.py` is the **parent**. The experiment's `run.py` is the
**child**, launched as a separate Python process with the same interpreter and
the repository root as its working directory:

```sh
python experiments/<name>/run.py /absolute/path/config.yml /absolute/path/output
```

| Responsibility | Parent | Experiment child |
| --- | --- | --- |
| Select the experiment and validate the config | Reads config and calls optional `resolve_parameters` | Reads the same config and applies its own defaults/resolver |
| Track execution | Creates the Lamin run, transform, and experiment-run record | Does not call `ln.track()` or write to the benchmarks instance |
| Perform the workload | Starts and supervises the child | Connects to the load target and performs writes, queries, and maintenance |
| Choose output location | Allocates a fresh temporary output directory and passes it as argument 2 | Uses that directory; never chooses a separate results location |
| Publish measurements | Polls completed observation folders and uploads records/artifacts | Writes files, closes them, then atomically publishes their folder |
| Publish runtime information | Saves `run_info.yml` values on the run record and in run metadata | Optionally publishes one immutable metadata snapshot |
| Report completion | Drains ready output, records success/failure, and finishes tracking | Exits zero on success, nonzero on failure |

The child receives the config path as argument 1, also exposed as
`BENCHMARK_CONFIG`. The parent sets the child's `LAMIN_LOAD_INSTANCE` and
`LAMIN_CURRENT_INSTANCE` to the load target. The parent's connection remains on
the tracking instance. A Notion experiment uses its Notion credentials instead
of connecting to the Lamin load target.

There are no setup/query stages, stdout notification messages, or child-side
upload calls. Logs go to inherited stdout/stderr and are streamed to the terminal
or Modal logs. The parent checks output about once per second between uploads.
The parent does not automatically run downstream analysis scripts or reset the
load database. The child controls any pauses, milestones, and database `ANALYZE`.

### Required experiment files

The runner requires these files in `experiments/<name>/`:

- `README.md`: YAML frontmatter with a nonempty `description`, followed by notes.
  The parent uses these to describe the experiment record.
- `run.py`: accepts exactly two positional arguments, `CONFIG OUTPUT_DIRECTORY`.

Also include `example.yml` as a tracked input example and `config/.gitignore`
to keep local run-specific configs untracked.

For example, an experiment README begins with:

```markdown
---
description: What this experiment measures
---

# My experiment

Workload and measurement definitions go here.
```

A config is a YAML mapping. `experiment` selects the repository-relative folder:

```yaml
experiment: experiments/my_experiment
n_rows: 1000
```

Include at least one non-null experiment parameter in addition to `experiment`.

Optional `name` sets the run display name; otherwise its UID is used. Other
experiment parameters become run-sheet features. An optional `parameters.py`
can expose `resolve_parameters(params: dict) -> dict`; the parent calls it before
tracking so effective defaults are recorded and invalid input fails early.
**The resolved dictionary is not written back to the config file.** The child
must use that same resolver/default logic when reading the supplied config.
Helpers and separate `analyze_*.py` scripts may live alongside `run.py`.

### Output directory layout

Only the following published paths are consumed by the parent:

```text
OUTPUT_DIRECTORY/
├── run_info.yml                  # Optional, one immutable runtime-info snapshot
├── .pending/                     # Unpublished work; ignored by the parent
│   └── temporary-observation/
└── observations/
    └── population_1_000/         # One completed observation; folder name = record name
        ├── manifest.yml         # Optional observation features
        ├── README.md            # Optional observation notes
        ├── timings.parquet      # Measurement artifact
        ├── query_plans.html      # Another measurement artifact
        └── nested/
            └── details.json     # Nested files also become artifacts
```

Files elsewhere in the output directory are not uploaded. There is no required
measurement filename or table schema: each experiment defines and documents its
own. Parquet files must be valid DataFrames; other measurement files are saved
as file artifacts. Use browser-readable HTML for reports, or formats such as
JSON, CSV, and images as appropriate. Close all files before publishing.

### Observation metadata files

`manifest.yml` is an optional YAML mapping. Each non-null top-level value becomes
an observation feature. For example:

```yaml
n_rows: 1000
starting_rows: 0
n_repetitions: 11
recorded_at: "2026-10-25T09:04:58Z"
```

Keys must be nonempty strings of at most 150 characters. Values can be booleans,
integers, finite floats, strings, homogeneous scalar lists, or JSON-compatible
mappings. Mixed-type lists are rejected; empty lists have dtype `list[str]`.
Null-valued observation keys are omitted. Keep a feature's dtype consistent
across observations in the same sheet. Quote timestamp strings: YAML otherwise
may parse a timestamp into a Python object instead of a JSON-compatible string.

`recorded_at` has special meaning: it must be an ISO 8601 string with an explicit
timezone, and is stored as a datetime. It should describe measurement completion,
not upload time. If omitted, the parent uses the observation record's creation
time. The helper can supply it automatically with `timestamped=True`.

`README.md` is optional UTF-8 Markdown; its full contents become the observation
record's notes. These two top-level control files are **not** uploaded as
artifacts. Every other file in the folder, including nested files, becomes an
artifact. The same filenames inside nested folders are ordinary artifacts.

An observation folder's name becomes its record name. It must be nonempty, at
most 150 characters, and must not start with a dot or contain `/` or `\`.
Symlinks and non-file/non-directory entries are rejected. Names must be unique
within an execution; the same name in different executions creates distinct
records. `artifact`, `experiment_run`, `observation`, and `observation_uid` are
reserved metadata keys managed by the parent. Do not put credentials in configs,
manifests, reports, or measurement files.

### Run information

`run_info.yml` is an optional YAML mapping for **observed runtime facts**, such as
the detected initial population:

```yaml
starting_rows: 0
```

The parent saves these values as feature columns on the experiment-run record
and under `Run.extra_data["run_info"]`. They do not modify `Run.params`, which
contains the effective input config. No observation or artifact is created.

Publish this file once per execution, before long-running work if the facts are
already known. It must be a nonempty mapping of non-null feature values, with
the same supported value types as manifests. It cannot overwrite any config
parameter or a parent-managed run column. Treat it as immutable after publication.
Write a temporary file in the output directory and atomically rename it to
`run_info.yml`, or use `write_run_information(output, values)`.

### Minimal child example

This runnable example measures creation of an in-memory list. Replace that
operation with the experiment's workload; the output protocol stays the same.

```python
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from benchmarks.config import read_config
from benchmarks.observations import (
    child_arguments,
    observation,
    write_run_information,
)


def main():
    args = child_arguments()
    params = read_config(args.config)
    write_run_information(args.output, {"starting_rows": 0})

    started = time.perf_counter()
    values = list(range(params["n_rows"]))
    elapsed_ms = (time.perf_counter() - started) * 1000

    with observation(args.output, "list_creation", {"n_rows": len(values)}) as folder:
        pd.DataFrame([{"latency_ms": elapsed_ms, "value": len(values)}]).to_parquet(
            folder / "timings.parquet", index=False
        )


if __name__ == "__main__":
    main()
```

No Lamin imports are needed for the filesystem handoff. Experiments targeting
Lamin can import it for their **load operations**, while leaving tracking and
measurement uploads to the parent.

### Publication and failure expectations

Write each observation into a temporary directory on the **same filesystem**,
then rename the directory to `OUTPUT_DIRECTORY/observations/<name>` only when
all measurements and metadata are complete. The `observation(...)` helper does
this using `.pending/`; an exception inside its context leaves work unpublished.
It also converts legacy `write_measurement` metadata into parquet columns before
publishing. New experiments can simply write their measurement files directly.

A rename is the completion signal. Once published, never modify, rename, or
delete that folder. There is no acknowledgement the child must wait for. Hidden
folders inside `observations/` are ignored and can also be used for staging.

The parent validates a ready folder, uploads its files, then creates the
observation record. Artifact keys include experiment, execution UID, observation
name, and relative filename; retries reuse files already saved at those keys.
The observation's direct artifact links contain only that observation's files.
Creator provenance is retained through `record.run` and the `experiment_run`
feature. Observations do not tag the entire execution or transform, which would
include other milestones' outputs in the Artifacts tab.

Malformed output stops the run. Other upload failures are retried up to three
attempts; persistent failure stops the child and marks the execution unsuccessful.
After either a successful or failed child exit, the parent drains ready output,
including `run_info.yml`. Uploaded observations survive later failures. Only
uploaded results are durable: unfinished work, or published output not yet
uploaded, can be lost if the container is killed. Local results directories are
retained on failure and removed after success; Modal's filesystem is ephemeral.

A new execution gets a fresh output directory and Lamin run. The parent neither
resumes the child process nor rolls back load-database changes. Each experiment
must define its own resume behavior; `record_scalability` detects existing rows.

## Tracking layout

Under each experiment page:

- `runs`: one record per execution, created before the child starts. Effective
  config values and published run information are feature columns. `environment`
  is `local` or `modal`, set by the parent. Each row links its Lamin run and
  transform. `succeeded` stays false until the child exits zero and uploads finish.
- `observations`: one record per published observation, linking its measurements.
  The first column, `observation`, links to the row's own record, followed by
  `recorded_at`. The run record does not duplicate observation artifact links.
- `analysis`: plots and summary tables produced by separately run analysis scripts.

`run_uid`, `started_at`, `succeeded`, `environment`, and `artifact` are managed by
the parent and cannot be supplied as config/run-info parameters. The config keys
`name` and `experiment` select runner behavior rather than run-sheet columns.
The environment is also saved in `Run.extra_data["environment"]`; Modal region,
provider, and container ID remain under `Run.extra_data["modal"]`. It is an
execution property, not an experiment input or an observation feature.

Historical scalar `n_rows` values are preserved as singleton lists when that
experiment's run sheet adopts milestone lists. The global scalar feature and
other experiments are unchanged. Omitted parameters do not delete historical
columns. Analyses support both historical run-linked artifacts and newer
observation-linked tables, including completed observations from failed runs.

## Local configurations

Copy an example into an experiment's ignored config folder and edit it:

```sh
cp experiments/record_scalability/example.yml experiments/record_scalability/config/my_run.yml
uv run python benchmarks/run_benchmark.py experiments/record_scalability/config/my_run.yml
```

The legacy root `config.yml` and `configs/` remain ignored. The optional local
`./config.yml` fallback is still accepted, but on a fresh checkout pass an example
or local config explicitly. Do not put credentials in tracked configs.

## Record scalability growth sweeps

`record_scalability` is a separate experiment type under `Load Benchmarks`, with
its own runs, observations, analysis sheets, and Transform history. Historical
`experiment_002` records, runs, and analyses stay under that experiment.
Use `experiment: experiments/record_scalability` for new executions. The load
database is independent of this tracking separation and is still resumed unless
explicitly reset.

```yaml
experiment: experiments/record_scalability
n_rows: [1000, 10000, 100000]
n_repetitions: 11
n_batch: 20
```

`n_rows` is a nonempty, strictly increasing list of positive integers. A positive
integer is accepted as a single milestone. Unsorted lists, duplicates, booleans,
negative values, zero, and fractional values fail before any database writes.
The targets are total intervention populations, not numbers of additional rows.

At startup, record scalability detects the existing benchmark population and saves
`starting_rows` on the experiment-run record and in run metadata, without a
startup observation. It checks catalog compatibility and, when
available, the workload metadata stored on the intervention type. If an old
config specifies `existing_rows`, it is an assertion against the detected count.
Milestones below that count are skipped; an equal milestone is evaluated without
inserting. If every milestone is below it, the execution fails after recording
its starting state. Only one writer should operate on a load instance at a time;
external population changes detected between milestones stop the execution.

For each milestone, the child inserts up to that population, refreshes planner
statistics with `ANALYZE`, runs queries, and publishes an observation named like
`record_scalability_2026-10-25T08:30:00Z_n_rows_10_000`. The timestamp is
the observation's completion time in UTC, at second precision. Maintenance runs in a dedicated subprocess so elevated
credentials and Django state do not leak into query timing. There is no README
in record scalability observations.

Each milestone contains all insert timing rows collected since this execution
started, query repetition and EXPLAIN tables, health, and one
`table_distribution.parquet` with one row per database table. Table names,
counts, sizes, and bloat are columns, not artifact features. These cumulative
insert tables overlap: analysis must use the latest completed snapshot for a
run rather than concatenate its milestone snapshots. A later run's insertion
history starts anew at its detected population.

In addition to the three UID lookups, each milestone times the latest 100
interventions ordered by the UTC `treatment_started_at` feature and by the native
`created_at` field. A sixth query returns the latest 100 interventions linked to
`query_gene` (default `gene_000`); the target must exist in the configured gene
catalog, and fewer than 100 matches are valid. A seventh query combines this gene
with `query_protocol` and a fixed treatment-date window, orders by newest treatment,
and returns up to 100 interventions with all features. Its defaults are
`protocol_B` and `2025-01-01T00:00:00Z <= treatment_started_at < 2025-07-01T00:00:00Z`;
see the full example config for configurable bounds. All seven operations use
`n_repetitions`. Existing populations
without the treatment timestamp must be reset or backfilled before resuming.
`query_plans.html` shows each Python call, its complete formatted data-SELECT
list, and matching plans; `explain.parquet` combines diagnostics for all seven.

## Provenance and Modal

Each execution records its repository commit, branch, dirty state, config path,
and editable dependency versions in `run.extra_data['git']`. Its transform tracks
the experiment's `run.py`, and its reference pins that file at the recorded
commit. Git links show the committed baseline; local changes and ignored
configs are not reconstructable from that link alone.

The project uses `lamindb==2.12.0`. Modal builds dependencies from
`requirements-modal.txt`, generated with:

```sh
uv export --frozen --no-dev --no-hashes --no-emit-project --output-file requirements-modal.txt
```

The launcher uploads `benchmarks/`, `experiments/`, `pyproject.toml`, and `uv.lock`.
Git metadata, environment files, virtual environments, and local config folders
are excluded. Only the selected local config is added explicitly. Default
compute region is `eu-west`; the run records its region, provider and container
ID. Keep resource settings consistent when comparing experiments.

Modal uses the same parent/child contract as local execution. The launcher starts
`benchmarks/run_benchmark.py` inside the container; that parent allocates the
output folder, starts the experiment's `run.py CONFIG OUTPUT_DIRECTORY`, and
uploads `run_info.yml` values and ready observations while the child runs. No
experiment-specific Modal function, deployment, or output-volume setup is needed.
The launcher explicitly marks the execution `modal`; the run sheet's
`environment` column and `Run.extra_data["environment"]` retain this value.
Direct local launches are marked `local`.

Before submitting a job, the launcher checks the config mapping, the
repository-relative experiment path, and the required `README.md` and `run.py`.
Both the selected config and experiment code must be under `benchmarks/` or
`experiments/`, the two uploaded source folders. A selected config inside an
ignored `config/` or `configs/` folder is added explicitly; other local configs
remain excluded.
Effective parameter validation runs in the container, where secrets are
available. In Modal environment `Benchmarks`, mount `lamin-benchmarks` with
`LAMIN_API_KEY`; Notion runs additionally use `--notion-secret notion-benchmarks`.
`--detach` lets the submitted Modal job continue after the local CLI disconnects.

## Reset explicitly

```sh
uv run python benchmarks/reset.py --yes
```

This is destructive and separate from running an experiment. It truncates load
instance user tables and clears write events, while retaining system tables.
Reset is untracked maintenance and creates no run or transform in the benchmarks
tracking instance.
It does not remove experimental indexes or columns. Experiments that add DDL
must clean it up, including on failure. Record scalability resumes existing data;
other experiments define their own population/cleanup behavior.

The experiment record can also be synced independently:

```sh
BENCHMARK_CONFIG=experiments/record_scalability/example.yml \
  uv run python benchmarks/experiment.py experiments/record_scalability
```

Use **records** for `Record` rows, **events** for `DbWrite` rows, and
**auxiliary table entries** for link and feature-value tables.
