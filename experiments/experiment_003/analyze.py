"""Boxplot of page-sort latency for succeeded experiment_003 runs.

One vertical box per record count. The x axis is categorical. Uses the latest
succeeded run at each size. Every timed repetition is included; the first is
the cold call.

Usage:
  python experiments/experiment_003/analyze.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import lamindb as ln
import pandas as pd
import plotly.graph_objects as go

from benchmarks.config import (
    BENCHMARK_INSTANCE,
    BENCHMARK_INSTANCE_ENV,
    instance_from_env,
)
from benchmarks.experiment import (
    LOAD_BENCHMARKS,
    RUNS_TYPE,
    record_analysis,
    record_by_name,
    require_child,
    run_uid_of,
)

NAME = Path(__file__).resolve().parent.name
QUERY_NAME = "page_date"
LATENCY = "latency_ms"
REPETITION = "repetition"
TABLE_KEY = f"benchmarks/analysis/{NAME}/page_sort.parquet"
PLOT_KEY = f"benchmarks/analysis/{NAME}/page_sort.svg"
TITLE = "RecordJson date page sort"
DESCRIPTION = (
    "Latency of a 50-row page ordered by a date stored in RecordJson. "
    "One box per record count. The first call is included."
)
NOTES = """# RecordJson date page sort

Shows how long a paged sheet takes to order Samples by `collected_on` when that date lives in `RecordJson`.

## What is analyzed

Succeeded runs only. The latest succeeded run at each record count is used. Each config started from an empty load database. The timed statement is the page `SELECT`, not `to_dataframe()`. Every repetition is plotted, including the cold first call. Spill comes from one `EXPLAIN (ANALYZE, BUFFERS)` and is not part of the box.

## How it is reported

One vertical box per record count. The x axis is the record count as a category, so the sizes are evenly spaced. Points are drawn on the box.

## Artifacts

- `benchmarks/analysis/experiment_003/page_sort.parquet`: one row per timed repetition, plus whether that size spilled the sort to disk.
- `benchmarks/analysis/experiment_003/page_sort.svg`: the boxplot.
"""


def main() -> None:
    instance = instance_from_env(BENCHMARK_INSTANCE_ENV, BENCHMARK_INSTANCE)
    ln.connect(instance)
    ln.track(params={"experiment": NAME, "analysis": "page_sort"})
    try:
        samples = load_samples(require_child(experiment_record(), RUNS_TYPE))
        print(samples.groupby("n_rows").size().to_string())
        table, plot = upload(samples, figure(samples), instance)
        analysis = record_analysis(
            experiment_record(),
            [table, plot],
            instance=instance,
            title=TITLE,
            description=DESCRIPTION,
            notes=NOTES,
        )
        print(f"table: https://lamin.ai/{instance}/artifact/{table.uid}")
        print(f"plot: https://lamin.ai/{instance}/artifact/{plot.uid}")
        print(f"analysis: https://lamin.ai/{instance}/record/{analysis.uid}")
    finally:
        ln.finish()


def experiment_record() -> ln.Record:
    parent = record_by_name(LOAD_BENCHMARKS)
    if parent is None:
        sys.exit(f"record type {LOAD_BENCHMARKS!r} was not found")
    experiment = record_by_name(NAME, type_record=parent)
    if experiment is None:
        sys.exit(f"experiment record {NAME!r} was not found")
    return experiment


def load_samples(runs: ln.Record) -> pd.DataFrame:
    feature = _artifact_feature(runs)
    selected = {record.uid: n_rows for record, _values, n_rows in latest_runs(runs)}
    spills = _spills(runs, feature, selected)
    rows: list[dict] = []
    for record in ln.Record.filter(type=runs, is_type=False):
        if record.uid not in selected:
            continue
        n_rows = selected[record.uid]
        print(f"using {record.name} for {n_rows} records")
        for artifact in measurements(record, feature):
            values = artifact.features.get_values()
            if values.get("query_name") != QUERY_NAME:
                continue
            if values.get("measurement_kind") not in (None, "page"):
                continue
            frame = artifact.load()
            if not isinstance(frame, pd.DataFrame):
                continue
            if LATENCY not in frame.columns or REPETITION not in frame.columns:
                continue
            for raw in frame.itertuples(index=False):
                row = raw._asdict()
                latency = pd.to_numeric(row.get(LATENCY), errors="coerce")
                repetition = pd.to_numeric(row.get(REPETITION), errors="coerce")
                if pd.isna(latency) or pd.isna(repetition):
                    continue
                rows.append(
                    {
                        "run_uid": run_uid_of(record),
                        "query_name": QUERY_NAME,
                        "n_rows": n_rows,
                        REPETITION: int(repetition),
                        "cold": int(repetition) == 1,
                        LATENCY: float(latency),
                        "spilled_to_disk": spills.get(n_rows),
                    }
                )
    if not rows:
        sys.exit(f"succeeded {NAME} runs have no {QUERY_NAME} measurements")
    return pd.DataFrame(rows)


def figure(samples: pd.DataFrame) -> go.Figure:
    labels = [str(int(size)) for size in sorted(samples["n_rows"].unique())]
    plot = go.Figure()
    for label in labels:
        subset = samples[samples["n_rows"] == int(label)]
        plot.add_trace(
            go.Box(
                y=subset[LATENCY],
                x=[label] * len(subset),
                name=label,
                marker_color="#4c78a8",
                line_color="#4c78a8",
                boxpoints="all",
                jitter=0.35,
                pointpos=0,
                showlegend=False,
            )
        )
    plot.update_xaxes(
        type="category",
        categoryorder="array",
        categoryarray=labels,
        title_text="Samples",
    )
    plot.update_yaxes(title_text="Latency (ms)")
    plot.update_layout(
        title=TITLE,
        template="plotly_white",
        height=480,
        width=720,
        margin={"t": 60, "b": 60},
        boxmode="overlay",
    )
    return plot


def upload(samples: pd.DataFrame, plot: go.Figure, instance: str):
    features = {
        _feature("experiment_name", "str"): NAME,
        _feature("statistic", "str"): "page_sort",
    }
    table = ln.Artifact.from_dataframe(
        samples,
        key=TABLE_KEY,
        description=(
            "Timed page-sort repetitions for the latest succeeded run at each "
            "record count, including whether the sort spilled to disk."
        ),
        features=features,
    ).save()
    with tempfile.TemporaryDirectory(prefix="benchmark-page-sort-") as raw:
        svg_path = Path(raw) / "page_sort.svg"
        plot.write_image(svg_path, format="svg")
        artifact = ln.Artifact(
            svg_path,
            key=PLOT_KEY,
            description="Boxplot of page-sort latency by record count.",
            features=features,
        ).save()
    print(f"uploaded table and plot to {instance}")
    return table, artifact


def latest_runs(runs: ln.Record) -> list[tuple[ln.Record, dict, int]]:
    chosen: dict[int, tuple[str, ln.Record, dict]] = {}
    for record in ln.Record.filter(type=runs, is_type=False):
        values = record.features.get_values()
        if values.get("succeeded") is not True:
            continue
        n_rows = _as_int(values.get("n_rows"), f"{record.name} n_rows")
        stamp = str(values.get("started_at") or "")
        current = chosen.get(n_rows)
        if current is None or stamp >= current[0]:
            chosen[n_rows] = (stamp, record, values)
    if not chosen:
        sys.exit(f"no succeeded runs for {NAME}")
    return [
        (record, values, n_rows)
        for n_rows, (_stamp, record, values) in sorted(chosen.items())
    ]


def _spills(
    runs: ln.Record, feature: ln.Feature, selected: dict[str, int]
) -> dict[int, bool]:
    found: dict[int, bool] = {}
    for record in ln.Record.filter(type=runs, is_type=False):
        if record.uid not in selected:
            continue
        for artifact in measurements(record, feature):
            values = artifact.features.get_values()
            if values.get("query_name") != f"explain_{QUERY_NAME}":
                continue
            if "spilled_to_disk" in values:
                found[selected[record.uid]] = bool(values["spilled_to_disk"])
    return found


def measurements(record: ln.Record, feature: ln.Feature) -> list[ln.Artifact]:
    seen: set[str] = set()
    artifacts = []
    for link in ln.models.RecordArtifact.filter(record=record, feature=feature):
        artifact = link.value
        if artifact.uid in seen:
            continue
        seen.add(artifact.uid)
        artifacts.append(artifact)
    return artifacts


def _artifact_feature(runs: ln.Record) -> ln.Feature:
    if runs.schema is None:
        sys.exit(f"{runs.name} has no schema")
    feature = runs.schema.members.filter(name="artifact").one_or_none()
    if feature is None:
        sys.exit(f"{runs.name} schema has no artifact column")
    return feature


def _feature(name: str, dtype: str) -> ln.Feature:
    found = ln.Feature.filter(name=name, is_type=False).first()
    if found is None:
        return ln.Feature(name=name, dtype=dtype).save()
    if found.dtype_as_str != dtype:
        sys.exit(f"feature {name!r} has dtype {found.dtype_as_str}, not {dtype}")
    return found


def _as_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        sys.exit(f"{label} must be an integer, got {value!r}")
    return value


if __name__ == "__main__":
    main()
