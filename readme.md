# LaminDB benchmarks

- The benchmark instance (`LAMIN_CURRENT_INSTANCE`, default `laminlabs/lamindb-benchmarks`) stores the experiment, its runs, and its analyses.
- The load instance (`LAMIN_LOAD_INSTANCE`, default `laminlabs/lamindb-load-test`) receives the writes and queries under test.

## Create, run, and analyze an experiment

An experiment is a folder under `experiments/`. `experiment_001` is the working example. Create the folder, run it against the load instance, then analyze the succeeded runs. Analysis is a separate step. It is not part of the benchmark run.

### Create

The folder needs three files.

`README.md` describes what the experiment measures. Do not describe the file layout. Put a short description in YAML frontmatter. That text becomes the experiment record's description. The body becomes its notes, and should explain each parameter.

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

`setup.py` creates that load on the load instance. `query.py` times the query and writes one measurement with `benchmarks.measurements.write_measurement`. The runner sets `BENCHMARK_RESULTS_DIR`. When that directory is unset, the query still runs and the measurement is not saved.

A config points at the folder and supplies the parameters:

```yaml
experiment: experiments/my_experiment
n_rows: 10
n_repetitions: 3
```

Every parameter other than `experiment` becomes a column on the run sheet. Keep runner settings such as instance names out of the config.

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
python benchmarks/run_benchmark.py configs/n_rows_10.yml
```

The runner connects to the benchmark instance and tracks the config. It then starts `setup.py` and `query.py` in separate processes against the load instance. Setup runs first. A failed setup does not run the query. After the processes exit, measurement files are uploaded and linked on a run row. `succeeded` is true only when the exit code is zero.

A size ladder wipes the load instance before every config, so sizes do not accumulate. `--yes` is required.

```bash
python benchmarks/batch_benchmarks.py --yes configs
```

A failed benchmark does not stop the ladder. A failed reset does.

### Analyze

Write the analysis script in the experiment folder and run it after succeeded runs exist. It connects to the benchmark instance, keeps run rows whose `succeeded` flag is true, and loads their measurement artifacts from LaminDB rather than from a local path.

Upload the summary table and any plot as artifacts. Then store one analysis record with `benchmarks.experiment.record_analysis`. Give that record a title, a short description, and notes. The notes should say what is analyzed and how it is reported. The title is a column on the analysis sheet. The description and notes are on the record. The row links the artifacts.

```bash
python experiments/experiment_001/analyze.py
python experiments/experiment_001/analyze_count.py
```

`analyze.py` summarizes latency by the number of records. `analyze_count.py` checks that the timed count matches the number of records created, and reports the latency range.
