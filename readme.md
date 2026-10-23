# LaminDB benchmarks

Two LaminDB instances, two processes. Do not combine them with `.using()`.

- The benchmark instance (`LAMIN_CURRENT_INSTANCE`, default `laminlabs/lamindb-benchmarks`) stores the experiment page, run rows, measurement artifacts, and analyses.
- The load instance (`LAMIN_LOAD_INSTANCE`, default `laminlabs/lamindb-load-test`) receives the writes and queries under test. A batch wipes it before every config.

`benchmarks/` is the generic runner. A new experiment is a new folder under `experiments/`. Do not edit `benchmarks/` to name that experiment, add its queries, or special-case its plots. If the contract below cannot express the measurement, stop and ask before changing the runner.

`experiments/experiment_001` is the small example: create plain records, time one count. `experiments/experiment_002` is the fuller example: typed records, several lookups, and facts captured during the run. `archive/` is the old study. Do not copy it into `benchmarks/`. Each experiment has a minimal tracked `example.yml`. Run-specific configs stay local in its ignored `config/` folder. Legacy root `configs/` and `config.yml` are also local and ignored.

Run every command from the repository root, with the project interpreter that already has lamindb. Do not start a temporary environment.

## Add an experiment

1. Create `experiments/<name>/` with `README.md`, `setup.py`, and `query.py`. The folder name becomes the Lamin record name. Do not rename it after the first sync.
2. Add a minimal `experiments/<name>/example.yml`. Put local configs in `experiments/<name>/config/`, with a `.gitignore` containing `*` and `!.gitignore`. Copy the example and change `n_rows` for each point on the ladder.
3. Run one config with `python benchmarks/run_benchmark.py experiments/<name>/config/<file>.yml`. That syncs the page, runs setup, then query, and uploads measurements.
4. For a ladder, `python benchmarks/batch_benchmarks.py --yes experiments/<name>/config`.
5. After succeeded runs exist, add analysis scripts in the same folder and run them yourself. The runner does not look for them.

## Folder

Required:

- `README.md` with YAML frontmatter. The `description` is the experiment record description. The body is its notes.
- `setup.py` creates the load. It runs first.
- `query.py` times the queries.

Optional, and not launched by the runner:

- `example.yml`, a minimal tracked config
- `config/.gitignore`, keeping run-specific `config/*.yml` local
- `analyze.py` and other `analyze_*.py` scripts
- helpers such as `observe.py`, imported by `setup.py` or `query.py`
- `parameters.py` with `resolve_parameters(params) -> dict`, a pure function that fills and validates defaults before tracking. The resolved values are saved in both the Lamin run parameters and its run-sheet row.

The runner starts each script by path, so the experiment folder is not on `sys.path`. A script that imports a sibling must insert its own directory. Use the folder name as the name prefix for records this experiment creates. Do not hardcode `experiment_001` or `experiment_002` in shared code.

The README body describes the measurement: what is timed, why the comparison stays fair, each parameter, each query with a one-line example, and each artifact. It does not describe the runner, the two instances, or how to invoke `benchmarks/`.

```markdown
---
description: Count latency as the number of records grows
---

# Count latency as the number of records grows

What the timed query measures, and why the comparison stays fair as the registry grows.

## Parameters

- `n_rows`: How many records to create before timing the query. Integer, at least 0.
```

Syncing the folder creates two sheets under the experiment page:

- `runs`: one row per benchmark run. Columns are the config scalars, the start time, whether it succeeded, and links to the measurement artifacts.
- `analysis`: one row per analysis execution.

```bash
python benchmarks/experiment.py experiments/my_experiment
```

That command does not touch the load instance. It still reads `./config.yml`, or `BENCHMARK_CONFIG` when set, so the runs sheet gets parameter columns. A benchmark run syncs the same page from the config it was given, so a separate sync is optional.

## Config

Run a tracked example directly, or copy it into the experiment's ignored config folder:

```sh
python benchmarks/run_benchmark.py experiments/experiment_002/example.yml

cp experiments/experiment_002/example.yml experiments/experiment_002/config/my_run.yml
python benchmarks/run_benchmark.py experiments/experiment_002/config/my_run.yml
```

Git stores the example and each folder's `.gitignore`, not the individual run configs. Existing local configs can still be used. On a fresh checkout, pass an example or a local config explicitly; the optional `./config.yml` fallback is not tracked.

```yaml
name: "My experiment: 10 records"
experiment: experiments/my_experiment
n_rows: 10
n_repetitions: 3
```

`experiment` is a folder path, resolved from the working directory, not from the YAML file's directory. It is not a run-sheet column.

Experiments with `parameters.py` record effective parameter values, including defaults omitted from YAML. Experiment 002 resolves its gene and molecule catalog sizes and link ranges, random seed, repetition count, and lookup batch size from the same defaults used by setup and queries.

`name` is the display name of both the Lamin run and its run record. It must be a non-empty string of at most 150 characters. Older configs without it fall back to the run uid. Repeated executions may share a display name; their run uids and run records remain distinct. The run sheet uses `name` as its display index and stores the execution uid separately in `run_uid`. The record explicitly links both its run and the run's transform, so they appear as attachments. Existing run-uid indexes are migrated without discarding the old uid values.

Run and analysis sheets store `started_at` with feature dtype `datetime`, as timezone-naive UTC for Hub rendering. Offset-aware start times are converted to UTC before the timezone is removed.

Every other value becomes a column. Rules:

- Values are scalars: `bool`, `int`, `float`, or `str`. No lists, no nested mappings, no nulls. A null key is skipped.
- Keep the Python type of a key stable across every config of that experiment. The first run fixes the column dtype.
- At least one scalar column is required.
- Do not use reserved keys: `run_uid`, `started_at`, `succeeded`, `artifact`.
- These keys are ignored as parameter columns: `name`, `experiment`, `experiment_script`, `setup_script`, `instance`, `load_instance`, `failed`. Do not put instance names in the config.
- Document every key in the experiment README, including its type, minimum, and default.

A batch runs every `.yml` and `.yaml` in the folder you pass. It wipes the load instance, then starts `run_benchmark.py` for that file. The next config starts only after that process exits. The batch script is not tracked and does not set `LAMIN_INITIATED_BY_RUN_UID`. Each config is recorded by its own `run_benchmark.py` run. The default folder is `./configs`, which is experiment 001, not every experiment. Pass the experiment's own config folder. To run a subset, copy only those files into an empty folder. A leftover YAML in that folder will also run. Filenames sort with numbers in order, so `n_rows_10.yml` runs before `n_rows_100.yml`.

A failed config does not stop the batch. A failed wipe does.

## Transform keys and Git references

The runner uses `benchmarks/experiments/<experiment folder name>` as its transform key, for example `benchmarks/experiments/experiment_002`. Each experiment has its own transform family even though they execute the same runner script. Config names label runs and result records; changing the record count does not change the transform key.

The runner allows uncommitted changes, untracked configs, and commits that have not been pushed. The experiment and config must be inside this repository, and the required source and dependency files must exist. These checks happen before setup or query writes to the load instance.

Each run's `reference` is the repository URL pinned to its full HEAD commit SHA. Its transform's `reference` links to `benchmarks/run_benchmark.py` at that commit. `run.extra_data["git"]` stores the repository, commit, branch, config and experiment URLs, `dirty`, and a `changes` list covering staged, unstaged, and untracked files. `published` indicates whether local `origin` remote-tracking refs contain the commit; it does not query GitHub. A dirty checkout or unpublished commit is flagged in the run log. Git links show the committed baseline, so local changes and untracked files are not included, and links to unpublished commits may be unavailable until pushed. These local changes are not archived by this metadata.

The project uses the published `lamindb==2.12.0` release. The metadata records versions of the six Lamin dependencies; if a dependency is installed as editable, it also records its Git commit and dirty status. A dirty dependency is flagged in the log and metadata. SSH origins are converted to browser URLs without including credentials. Modal installs the default project dependencies exported from `uv.lock` into `requirements-modal.txt`. Regenerate that file after dependency changes with `uv export --frozen --no-dev --no-hashes --no-emit-project --output-file requirements-modal.txt`.

Git references describe the checkout used when a new run starts. They do not use a moving branch URL.

The Modal launcher uploads only `benchmarks/`, `experiments/`, `pyproject.toml`, and `uv.lock`, respecting the root `.gitignore` and excluding `.git` directories and local `config/` and `configs/` folders. It explicitly adds the selected config when it is in one of those folders. It uses `requirements-modal.txt` to build the environment. Its local entrypoint captures the repository URL, commit, branch, dirty status, changed paths, and publication status for each launch, then passes that snapshot to the remote runner through `BENCHMARK_GIT_PROVENANCE`. Dependency versions are collected inside the remote environment. Local runs continue to read Git directly. Configs passed to Modal must be inside `benchmarks/` or `experiments/`.

The Modal launcher pins compute to `eu-west` by default; pass `--region` to override it. Each remote invocation logs `MODAL_REGION` and `MODAL_CLOUD_PROVIDER`, and the benchmark run saves these plus its container ID in `run.extra_data["modal"]`. Keep the region, CPU, and memory settings constant across a size ladder. Experiment 002 uses 11 repetitions and lookup batch size 20 by default, saves timings for each insertion batch of up to 1,000 records, and runs `ANALYZE` after population before query timing.

## Setup and query

Both connect to `LAMIN_LOAD_INSTANCE`, or `laminlabs/lamindb-load-test` when that is unset. Read parameters with `benchmarks.config.read_config` and `require_int`. The runner sets `BENCHMARK_CONFIG` to the file it was given.

Do not call `ln.track()` or `ln.finish()` on the load instance. Finish fails while that instance's Storage row is missing. The parent run on the benchmark instance is the tracked execution. The runner removes `LAMIN_INITIATED_BY_RUN_UID` from the child environment. Do not put it back.

Only `reset.py` and the batch wipe the load instance. One call to `run_benchmark.py` does not. A wipe truncates user tables, including records, features, and schemas, and clears write events. It does not drop indexes, columns, or extra tables. Bionty tables, migrations, users, storage, runs, and transforms are kept. Recreate types, features, and catalogs in `setup.py` on every run. Do not assume rows from a smaller config are still there. If the experiment creates an index, column, or table, drop it before the process exits, including on failure. A leftover index on `lamindb_recordjson`, or a leftover column or index on `lamindb_recordrecord`, would change later experiments.

`setup.py` may write measurements too. The insert is the usual one. `query.py` runs only if setup exits 0. After both exit, the runner uploads every measurement in `BENCHMARK_RESULTS_DIR`, even when the exit code is non-zero. `succeeded` is true only for exit code 0.

Time calls with `benchmarks.timing.time_calls`. The first repetition is the cold call. Later repetitions should reuse the same sampled ids. The timed function's return value goes in the measurement `value` column. Return the actual row count, not the number of ids you asked for.

Facts that disappear when the load instance is wiped must be written during the run: insert time, write-event counts, auxiliary-table counts, disk, cache, and `EXPLAIN`. Analysis must not reconnect to the load instance to reconstruct them.

If the experiment needs server SQL, keep it in the experiment folder. Use LaminDB's Django cursor. A fresh psycopg connection fails RLS with `JWT is not set`. Do not put a literal `%` in SQL passed to psycopg; pass the pattern as a parameter. Django may log `set_token` and the real `SELECT` as one string. Split on semicolons and skip `set_token` before `EXPLAIN`. Leave RLS enabled. Use the normal schema and indexes. A scan with indexes removed is a different experiment, not a flag in this runner.

Use these words: **records** for `Record` rows, **events** for `DbWrite` rows, **auxiliary table entries** for link tables. Do not say "records loaded" or "write amplification".

## Measurements

Write them with `benchmarks.measurements.write_measurement`. If `BENCHMARK_RESULTS_DIR` is unset, the call returns `None` and the script still finishes. That is how a query can be tried outside the runner.

The parquet has exactly three columns: `repetition`, `latency_ms`, `value`. Do not add columns. Extra keys in a row are dropped. A one-shot fact still uses one row, for example repetition 1 and the fact in `value` or in features.

```python
write_measurement(
    "uid_lookup",
    rows_from_timed(timed),
    key=f"benchmarks/measurements/{NAME}/uid_lookup.parquet",
    description="Timed uid lookup.",
    features={
        "measurement_kind": "lookup",
        "query_name": "uid_lookup",
        "n_rows": n_rows,
    },
)
```

The key is a relative `.parquet` path. The runner inserts the run uid after the experiment folder name, so `benchmarks/measurements/<name>/uid_lookup.parquet` is stored as `benchmarks/measurements/<name>/<run uid>/uid_lookup.parquet`. The file stem is the `name` argument. No slashes.

Feature values are Python `bool`, `int`, `float`, or `str`. Not numpy scalars, not `None`, not lists. Omit a missing number instead of storing null. Feature identity on the benchmark instance is the name, not the experiment. Reuse a name only with the same dtype. Pick a new name rather than asking the runner for a per-experiment feature namespace.

Tag each file so analysis can tell lookups from other facts:

- `measurement_kind`: `lookup`, `insert`, `explain`, `health`, or `distribution`
- `query_name`: stable name of that fact

Analysis that plots latency must ignore files whose kind is not `lookup`.

## Analysis

An analysis script connects to the benchmark instance, calls `ln.track()` and `ln.finish()`, and reads artifacts from LaminDB. Do not read the local parquet the runner already uploaded.

Find the experiment with `record_by_name` under `LOAD_BENCHMARKS`, then its `runs` child with `require_child`. Keep rows whose `succeeded` feature is true. Load artifacts through the run's `artifact` feature. For a size ladder, use the latest succeeded run at each `n_rows`, because each config started from an empty database. Say so in the notes. Pooling every historical run mixes different repetition counts. Use `record.uid` when selecting records and `run_uid_of(record)` for an output table's `run_uid`; display names can repeat.

Upload a table and an SVG plot. Then:

```python
record_analysis(
    experiment,
    [table, plot],
    instance=instance,
    title="...",
    description="...",
    notes=NOTES,
)
```

`title` is a column on the analysis sheet. `description` and `notes` are the record description and readme. The notes say what was analyzed, how it is reported, and what each artifact is. `record_analysis` requires the active `ln.track()` run.

Save plots with `figure.write_image(path, format="svg")`. Do not upload HTML. For a size ladder, treat record count as a category so the sizes are evenly spaced. Use bars or unconnected markers. Put the spread in error bars or a boxplot. Do not connect sizes with a line unless asked.

Suggested keys: `benchmarks/analysis/<name>/<plot>.parquet` and the matching `.svg`.

## Scripts

None of these name a particular experiment.

- `benchmarks/experiment.py <folder>` syncs the page. It does not touch the load instance.
- `benchmarks/run_benchmark.py [config]` tracks one config on the benchmark instance, then runs that folder's `setup.py` and `query.py` on the load instance.
- `benchmarks/batch_benchmarks.py --yes [folder]` wipes the load instance, then starts `run_benchmark.py` for every YAML file in that folder. It does not record a batch run.
- `benchmarks/reset.py --yes` wipes the load instance. It refuses to run without `--yes`.

`benchmarks/config.py`, `benchmarks/measurements.py`, and `benchmarks/timing.py` are imported by experiments. `benchmarks/setup.py` only creates plain records. An experiment does not have to use it.

## Do not change benchmarks for

Solve these in the experiment folder:

- another query, record type, or feature dtype
- insert timing, `EXPLAIN`, table sizes, or server health
- another analysis or plot
- a helper module imported by setup or query
- a fact that does not fit the three parquet columns: put it in features, or write one measurement per item
- a subset of a ladder: copy those YAML files to an empty folder

Stop and ask before changing `benchmarks/` if the experiment needs a third process the runner must launch, a measurement column other than `repetition`, `latency_ms`, and `value`, a non-scalar config value, or a second load instance.
