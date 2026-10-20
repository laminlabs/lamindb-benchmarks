"""Summarize feature-lookup latency for succeeded experiment_002 runs.

The first repetition is the first call and is kept out of the plot.
Warm repetitions are summarized as median latency, with the 5th and 95th
percentiles as error bars. Uploads the table and an SVG figure, then stores
an analysis record that links both.

Usage:
  python experiments/experiment_002/analyze.py
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
from experiments.experiment_002.workload import matches_analysis_workload

NAME = Path(__file__).resolve().parent.name
LATENCY = "latency_ms"
REPETITION = "repetition"
TABLE_KEY = f"benchmarks/analysis/{NAME}/latency_median_p95.parquet"
PLOT_KEY = f"benchmarks/analysis/{NAME}/latency_median_p95.svg"
TITLE = "Feature lookup latency, median and p95"
DESCRIPTION = (
    "Warm median and 95th percentile latency of uid lookups as the number "
    "of intervention records grows."
)
NOTES = """# Feature lookup latency, median and p95

Compares how long each uid lookup takes after a controlled number of intervention records have been created.

## What is analyzed

Succeeded runs only. Each run times three lookups on the same sampled records: one record by uid, that record with its features, and a batch of records with their features. The first repetition is the first call. Later repetitions are the warm calls.

## How it is reported

One table row per lookup and number of records. The first call is the first repetition. The median, 5th percentile, and 95th percentile use only the warm repetitions. The plot is an unconnected scatter of the warm median against the number of records, with a log scale on the x axis. Error bars span the 5th to the 95th percentile. Points are not joined.

## Artifacts

- `benchmarks/analysis/experiment_002/latency_median_p95.parquet`: the summary table. One row per lookup and record count.
- `benchmarks/analysis/experiment_002/latency_median_p95.svg`: the scatter figure.
"""
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
SYMBOLS = {
    "uid_lookup": "circle",
    "uid_with_features": "square",
    "uid_batch_with_features": "diamond",
}


def main() -> None:
    instance = instance_from_env(BENCHMARK_INSTANCE_ENV, BENCHMARK_INSTANCE)
    ln.connect(instance)
    ln.track(params={"experiment": NAME, "analysis": "latency_median_p95"})
    try:
        experiment = experiment_record()
        samples = load_samples(require_child(experiment, RUNS_TYPE))
        summary = summarize(samples)
        print(summary.to_string(index=False))
        table, plot = upload(summary, latency_figure(summary), instance)
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
    rows: list[dict] = []
    succeeded = 0
    for record in ln.Record.filter(type=runs, is_type=False):
        values = record.features.get_values()
        if values.get("succeeded") is not True:
            continue
        if not matches_analysis_workload((record.run.params or {}) if record.run else {}):
            continue
        succeeded += 1
        n_rows = _as_int(values.get("n_rows"), f"{record.name} n_rows")
        artifacts = _measurements(record, feature)
        if not artifacts:
            print(f"[warn] {record.name} succeeded but has no measurement artifact")
            continue
        for artifact in artifacts:
            values = artifact.features.get_values()
            query_name = _query_name(artifact, values)
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
            n_uids = _optional_int(values.get("n_uids"))
            for raw in frame.itertuples(index=False):
                row = raw._asdict()
                latency = pd.to_numeric(row.get(LATENCY), errors="coerce")
                repetition = pd.to_numeric(row.get(REPETITION), errors="coerce")
                if pd.isna(latency) or pd.isna(repetition):
                    continue
                rows.append(
                    {
                        "run_uid": run_uid_of(record),
                        "query_name": query_name,
                        "n_rows": n_rows,
                        "n_uids": n_uids,
                        REPETITION: int(repetition),
                        LATENCY: float(latency),
                    }
                )
    if succeeded == 0:
        sys.exit(f"no succeeded runs for {NAME}")
    if not rows:
        sys.exit(f"succeeded {NAME} runs have no {LATENCY} measurements")
    return pd.DataFrame(rows)


def summarize(samples: pd.DataFrame) -> pd.DataFrame:
    """Warm median and p95, plus the first call, by lookup and n_rows."""
    rows = []
    grouped = samples.groupby(["query_name", "n_rows"], sort=True)
    for (query_name, n_rows), group in grouped:
        first_call = group[group[REPETITION] == 1][LATENCY]
        warm = group[group[REPETITION] > 1][LATENCY]
        if warm.empty:
            print(f"[warn] {query_name} at {n_rows} rows has no warm repetitions")
            continue
        rows.append(
            {
                "query_name": query_name,
                "n_rows": int(n_rows),
                "n_uids": _group_uids(group, query_name, int(n_rows)),
                "n_runs": int(group["run_uid"].nunique()),
                "n_warm": int(len(warm)),
                "latency_first_call_ms": float(first_call.median()) if not first_call.empty else None,
                "latency_median_ms": float(warm.median()),
                "latency_p05_ms": float(warm.quantile(0.05)),
                "latency_p95_ms": float(warm.quantile(0.95)),
            }
        )
    if not rows:
        sys.exit(f"succeeded {NAME} runs have no warm repetitions")
    return pd.DataFrame(rows)


def latency_figure(summary: pd.DataFrame) -> go.Figure:
    """Unconnected median markers. Error bars span the warm 5th to 95th percentile."""
    figure = go.Figure()
    for query_name in summary["query_name"].drop_duplicates():
        stats = summary[summary["query_name"] == query_name]
        label = LABELS.get(query_name, query_name)
        color = COLORS.get(query_name, "#333333")
        median = stats["latency_median_ms"]
        figure.add_trace(
            go.Scatter(
                x=stats["n_rows"],
                y=median,
                mode="markers",
                name=label,
                marker={"color": color, "size": 10, "symbol": SYMBOLS.get(query_name, "circle")},
                error_y=_error_bar(median, stats["latency_p05_ms"], stats["latency_p95_ms"], color),
                hovertemplate=(
                    "n_rows=%{x}<br>median=%{y:.3f} ms<extra>" + label + "</extra>"
                ),
            )
        )
    tick_rows = list(summary["n_rows"].drop_duplicates())
    figure.update_layout(
        title=TITLE,
        xaxis_title="Number of intervention records",
        yaxis_title="Latency (ms)",
        template="plotly_white",
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "x": 0},
        margin={"t": 110},
    )
    figure.update_xaxes(
        type="log",
        tickmode="array",
        tickvals=tick_rows,
        ticktext=[str(n_rows) for n_rows in tick_rows],
    )
    return figure


def _error_bar(
    median: pd.Series, low: pd.Series, high: pd.Series, color: str
) -> dict:
    center = pd.to_numeric(median, errors="coerce")
    below = (center - pd.to_numeric(low, errors="coerce")).clip(lower=0)
    above = (pd.to_numeric(high, errors="coerce") - center).clip(lower=0)
    return {
        "type": "data",
        "symmetric": False,
        "array": above.tolist(),
        "arrayminus": below.tolist(),
        "color": color,
        "thickness": 1.4,
        "width": 6,
    }


def upload(
    summary: pd.DataFrame, figure: go.Figure, instance: str
) -> tuple[ln.Artifact, ln.Artifact]:
    features = {
        _feature("experiment_name", "str"): NAME,
        _feature("statistic", "str"): "latency_median_p95",
    }
    table = ln.Artifact.from_dataframe(
        summary,
        key=TABLE_KEY,
        description=(
            "Warm lookup latency by record count: first call, median, "
            "5th percentile, and 95th percentile."
        ),
        features=features,
    ).save()
    with tempfile.TemporaryDirectory(prefix="benchmark-analysis-") as raw:
        svg_path = Path(raw) / "latency_median_p95.svg"
        figure.write_image(svg_path, format="svg")
        plot = ln.Artifact(
            svg_path,
            key=PLOT_KEY,
            description=(
                "Unconnected scatter of warm median lookup latency. "
                "Error bars span the 5th to the 95th percentile."
            ),
            features=features,
        ).save()
    print(f"uploaded table and plot to {instance}")
    return table, plot


def _feature(name: str, dtype: str) -> ln.Feature:
    feature = ln.Feature.filter(name=name, is_type=False).first()
    if feature is None:
        return ln.Feature(name=name, dtype=dtype).save()
    if feature.dtype_as_str != dtype:
        sys.exit(f"feature {name!r} has dtype {feature.dtype_as_str}, not {dtype}")
    return feature


def _measurements(record: ln.Record, feature: ln.Feature) -> list[ln.Artifact]:
    seen: set[str] = set()
    artifacts = []
    for link in ln.models.RecordArtifact.filter(record=record, feature=feature):
        artifact = link.value
        if artifact.uid in seen:
            continue
        seen.add(artifact.uid)
        artifacts.append(artifact)
    return artifacts


def _query_name(artifact: ln.Artifact, values: dict | None = None) -> str:
    if values is None:
        values = artifact.features.get_values()
    value = values.get("query_name")
    if isinstance(value, str) and value.strip():
        return value.strip()
    stem = Path(artifact.key or artifact.uid).stem
    return stem or artifact.uid


def _group_uids(group: pd.DataFrame, query_name: str, n_rows: int) -> int | None:
    values = {int(value) for value in group["n_uids"].dropna().unique()}
    if not values:
        return None
    if len(values) > 1:
        print(f"[warn] {query_name} at {n_rows} rows sampled {sorted(values)} uids")
    return max(values)


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _as_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        sys.exit(f"{label} must be an integer, got {value!r}")
    return value


if __name__ == "__main__":
    main()
