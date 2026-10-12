# LaminDB benchmarks

- The benchmark instance (`LAMIN_CURRENT_INSTANCE`, default `laminlabs/lamindb-benchmarks`) stores the experiment, its runs, and its analyses.
- The load instance (`LAMIN_LOAD_INSTANCE`, default `laminlabs/lamindb-load-test`) receives the writes and queries under test.

## Scripts

None of these scripts name a particular experiment. The experiment is always the folder given by the config or the command.

- `benchmarks/experiment.py <folder>` syncs that folder to a page under Load Benchmarks. It does not touch the load instance.
- `benchmarks/run_benchmark.py [config]` runs one config. It tracks on the benchmark instance, then starts the folder's `setup.py` and `query.py` as separate processes on the load instance.
- `benchmarks/batch_benchmarks.py --yes [folder]` wipes the load instance, then runs every YAML file in that folder. A failed run does not stop the batch. A failed wipe does.
- `benchmarks/reset.py --yes` wipes the load instance. It refuses to run without `--yes`.

`benchmarks/config.py`, `benchmarks/measurements.py`, and `benchmarks/timing.py` are helpers. An experiment imports them. They are not run on their own. `benchmarks/setup.py` is a helper for creating plain records. An experiment does not have to use it.

## Experiment folder

The runner expects a folder with these files:

- `README.md` describes the measurement. A YAML frontmatter `description` becomes the experiment record's description. The body becomes its notes and should explain each parameter. Do not describe the file layout.
- `setup.py` creates the load on the load instance. It reads the same config the runner passed, through `BENCHMARK_CONFIG` or `./config.yml`.
- `query.py` times the queries on the load instance. It writes one measurement per query with `benchmarks.measurements.write_measurement`. The runner sets `BENCHMARK_RESULTS_DIR`. When that directory is unset, the query still runs and the measurement is not saved.

`setup.py` runs first. A failed setup does not run the query. Both connect to `LAMIN_LOAD_INSTANCE`, or `laminlabs/lamindb-load-test` when that is unset. Analysis scripts are optional and are not part of the run. Put them in the same folder and run them separately after succeeded runs exist.

A config is a YAML file. `experiment` is the folder path. Every other scalar becomes a column on the run sheet. Keep runner settings, such as instance names, out of the config. Configs may live anywhere. A batch folder must contain only the YAML files for one experiment, because the batch runs every file in that folder.

```yaml
experiment: experiments/my_experiment
n_rows: 10
n_repetitions: 3
```

## Create, run, and analyze an experiment

Create the folder, run it against the load instance, then analyze the succeeded runs.

### Create

The README frontmatter is the description. The body is the notes.

```markdown
---
description: Count latency as the number of records grows
---

# Count latency as the number of records grows

What the timed query measures, and why the comparison stays fair as the registry grows.

## Parameters

- `n_rows`: How many records to create before timing the query.
- `n_repetitions`: How many times to time the query.
```

On the benchmark instance the experiment is a page under Load Benchmarks. Syncing the folder creates two sheets under that page:

- `runs` holds one row per benchmark run: the parameters, the start time, whether it succeeded, and links to the measurement artifacts.
- `analysis` holds one row per analysis execution.

Sync the page without touching the load instance. The command still reads `./config.yml`, or `BENCHMARK_CONFIG` when that is set, so the runs sheet gets its parameter columns.

```bash
python benchmarks/experiment.py experiments/my_experiment
```

A benchmark run syncs the same page before it starts.

### Run

One config:

```bash
python benchmarks/run_benchmark.py experiments/my_experiment/config/n_rows_10.yml
```

The runner connects to the benchmark instance and tracks the config. It then starts `setup.py` and `query.py` in separate processes against the load instance. After the processes exit, measurement files are uploaded and linked on a run row. `succeeded` is true only when the exit code is zero.

A size ladder wipes the load instance before every config, so sizes do not accumulate. `--yes` is required. The folder must contain only that experiment's configs.

```bash
python benchmarks/batch_benchmarks.py --yes experiments/my_experiment/config
```

### Analyze

Write the analysis script in the experiment folder and run it after succeeded runs exist. It connects to the benchmark instance, keeps run rows whose `succeeded` flag is true, and loads their measurement artifacts from LaminDB rather than from a local path.

Upload the summary table and any plot as artifacts. Then store one analysis record with `benchmarks.experiment.record_analysis`. Give that record a title, a short description, and notes. The notes should say what is analyzed and how it is reported. The title is a column on the analysis sheet. The description and notes are on the record. The row links the artifacts.

```bash
python experiments/my_experiment/analyze.py
```

The runner does not look for an analysis script. Each experiment supplies its own.
