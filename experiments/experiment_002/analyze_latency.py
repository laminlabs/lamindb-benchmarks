"""Boxplots of query latency for succeeded experiment_002 runs.

One facet per lookup. The boxes are vertical. The shared x axis is the
configured record count, treated as a category so the sizes are evenly
spaced. Uses the latest succeeded run at each size. Every timed repetition
is included; the first is the cold call.

Usage:
  python experiments/experiment_002/analyze_latency.py
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
from plotly.subplots import make_subplots

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
)

NAME = Path(__file__).resolve().parent.name
LATENCY = "latency_ms"
REPETITION = "repetition"
LOOKUPS = (
    "uid_lookup",
    "uid_with_features",
    "uid_batch_with_features",
)
LABELS = {
    "uid_lookup": "lookup",
    "uid_with_features": "with features",
    "uid_batch_with_features": "batch with features",
}
COLORS = {
    "uid_lookup": "#1f77b4",
    "uid_with_features": "#ff7f0e",
    "uid_batch_with_features": "#2ca02c",
}
TABLE_KEY = f"benchmarks/analysis/{NAME}/latency_boxplot.parquet"
PLOT_KEY = f"benchmarks/analysis/{NAME}/latency_boxplot.svg"
TITLE = "Query latency by record count"
DESCRIPTION = (
    "Vertical boxplots of every timed lookup repetition. One facet per "
    "query. The x axis is the configured record count as a category."
)
NOTES = """# Query latency by record count

Shows the spread of timed lookup calls at each configured size.

## What is analyzed

Succeeded runs only. The latest succeeded run at each record count is used, so a later rerun replaces the earlier one. All three lookups are included: one record by uid, that record with its features, and a batch of records with their features. Every repetition is plotted. The first repetition is the cold call and can sit above the rest of the box.

## How it is reported

Three vertical facets, one per query, with a shared categorical x axis. The categories are the configured record counts, evenly spaced rather than placed on a log scale. Each box is the timed repetitions of that query at that size. Points are drawn on the box.

## Artifacts

- `benchmarks/analysis/experiment_002/latency_boxplot.parquet`: one row per timed repetition, with the run, query, record count, and latency.
- `benchmarks/analysis/experiment_002/latency_boxplot.svg`: the three-facet boxplot.
"""


def main() -> None:
    instance = instance_from_env(BENCHMARK_INSTANCE_ENV, BENCHMARK_INSTANCE)
    ln.connect(instance)
    ln.track(params={"experiment": NAME, "analysis": "latency_boxplot"})
    try:
        experiment = experiment_record()
        samples = load_samples(require_child(experiment, RUNS_TYPE))
        print(samples.groupby(["query_name", "n_rows"]).size().to_string())
        table, plot = upload(samples, latency_figure(samples), instance)
        analysis = record_analysis(
            experiment,
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
    if runs.schema is None:
        sys.exit(f"{runs.name} has no schema")
    feature = runs.schema.members.filter(name="artifact").one_or_none()
    if feature is None:
        sys.exit(f"{runs.name} schema has no artifact column")
    selected = {record.name: n_rows for record, _values, n_rows in latest_runs(runs)}
    if not selected:
        sys.exit(f"no succeeded runs for {NAME}")
    rows: list[dict] = []
    for record in ln.Record.filter(type=runs, is_type=False):
        if record.name not in selected:
            continue
        n_rows = selected[record.name]
        print(f"using {record.name} for {n_rows} records")
        for artifact in measurements(record, feature):
            values = artifact.features.get_values()
            query_name = query_name_of(artifact, values)
            if query_name not in LABELS:
                continue
            kind = values.get("measurement_kind")
            if isinstance(kind, str) and kind != "lookup":
                continue
            frame = artifact.load()
            if not isinstance(frame, pd.DataFrame):
                continue
            if LATENCY not in frame.columns or REPETITION not in frame.columns:
                print(f"[warn] {artifact.key or artifact.uid} has no latency repetitions")
                continue
            for raw in frame.itertuples(index=False):
                row = raw._asdict()
                latency = pd.to_numeric(row.get(LATENCY), errors="coerce")
                repetition = pd.to_numeric(row.get(REPETITION), errors="coerce")
                if pd.isna(latency) or pd.isna(repetition):
                    continue
                rows.append(
                    {
                        "run_uid": record.name,
                        "query_name": query_name,
                        "n_rows": n_rows,
                        REPETITION: int(repetition),
                        "cold": int(repetition) == 1,
                        LATENCY: float(latency),
                    }
                )
    if not rows:
        sys.exit(f"succeeded {NAME} runs have no {LATENCY} measurements")
    return pd.DataFrame(rows)


def latency_figure(samples: pd.DataFrame) -> go.Figure:
    """Three vertical boxplot facets with one shared categorical x axis."""
    labels = [str(size) for size in sorted(samples["n_rows"].unique())]
    figure = make_subplots(
        rows=len(LOOKUPS),
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.06,
        subplot_titles=tuple(LABELS[name] for name in LOOKUPS),
    )
    for index, query_name in enumerate(LOOKUPS, start=1):
        group = samples[samples["query_name"] == query_name]
        color = COLORS[query_name]
        for label in labels:
            size = int(label)
            subset = group[group["n_rows"] == size]
            if subset.empty:
                continue
            figure.add_trace(
                go.Box(
                    y=subset[LATENCY],
                    x=[label] * len(subset),
                    name=label,
                    marker_color=color,
                    line_color=color,
                    boxpoints="all",
                    jitter=0.35,
                    pointpos=0,
                    showlegend=False,
                    hovertemplate=(
                        "records=%{x}<br>latency=%{y:.3f} ms<extra>"
                        + LABELS[query_name]
                        + "</extra>"
                    ),
                ),
                row=index,
                col=1,
            )
        figure.update_yaxes(title_text="Latency (ms)", row=index, col=1)
    figure.update_xaxes(
        type="category",
        categoryorder="array",
        categoryarray=labels,
    )
    figure.update_xaxes(title_text="Intervention records", row=len(LOOKUPS), col=1)
    figure.update_layout(
        title=TITLE,
        template="plotly_white",
        height=860,
        width=760,
        margin={"t": 80, "b": 60},
        boxmode="overlay",
    )
    return figure


def upload(
    samples: pd.DataFrame, figure: go.Figure, instance: str
) -> tuple[ln.Artifact, ln.Artifact]:
    features = {
        feature("experiment_name", "str"): NAME,
        feature("statistic", "str"): "latency_boxplot",
    }
    table = ln.Artifact.from_dataframe(
        samples.sort_values(["query_name", "n_rows", REPETITION]),
        key=TABLE_KEY,
        description=(
            "Timed lookup repetitions for the latest succeeded run at each "
            "record count. Includes the cold first call."
        ),
        features=features,
    ).save()
    with tempfile.TemporaryDirectory(prefix="benchmark-latency-box-") as raw:
        svg_path = Path(raw) / "latency_boxplot.svg"
        figure.write_image(svg_path, format="svg")
        plot = ln.Artifact(
            svg_path,
            key=PLOT_KEY,
            description=(
                "Three vertical boxplot facets of lookup latency, with a "
                "shared categorical x axis of record counts."
            ),
            features=features,
        ).save()
    print(f"uploaded table and plot to {instance}")
    return table, plot


def latest_runs(runs: ln.Record) -> list[tuple[ln.Record, dict, int]]:
    chosen: dict[int, tuple[str, ln.Record, dict]] = {}
    for record in ln.Record.filter(type=runs, is_type=False):
        values = record.features.get_values()
        if values.get("succeeded") is not True:
            continue
        n_rows = as_int(values.get("n_rows"), f"{record.name} n_rows")
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


def measurements(record: ln.Record, feature_col: ln.Feature) -> list[ln.Artifact]:
    seen: set[str] = set()
    artifacts = []
    for link in ln.models.RecordArtifact.filter(record=record, feature=feature_col):
        artifact = link.value
        if artifact.uid in seen:
            continue
        seen.add(artifact.uid)
        artifacts.append(artifact)
    return artifacts


def query_name_of(artifact: ln.Artifact, values: dict) -> str:
    value = values.get("query_name")
    if isinstance(value, str) and value.strip():
        return value.strip()
    stem = Path(artifact.key or artifact.uid).stem
    return stem or artifact.uid


def feature(name: str, dtype: str) -> ln.Feature:
    found = ln.Feature.filter(name=name, is_type=False).first()
    if found is None:
        return ln.Feature(name=name, dtype=dtype).save()
    if found.dtype_as_str != dtype:
        sys.exit(f"feature {name!r} has dtype {found.dtype_as_str}, not {dtype}")
    return found


def as_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        sys.exit(f"{label} must be an integer, got {value!r}")
    return value


if __name__ == "__main__":
    main()
