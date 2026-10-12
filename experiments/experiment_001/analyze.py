"""Summarize succeeded experiment_001 runs on the benchmark instance.

Reads run records whose succeeded flag is true, loads their measurement
artifacts, and summarizes latency by number of rows. Uploads the summary
table and an SVG figure, then stores an analysis record that links both.

Usage:
  python experiments/experiment_001/analyze.py
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
)

NAME = Path(__file__).resolve().parent.name
LATENCY = "latency_ms"
TABLE_KEY = f"benchmarks/analysis/{NAME}/latency_by_n_rows.parquet"
PLOT_KEY = f"benchmarks/analysis/{NAME}/latency_by_n_rows.svg"
MEAN_COLOR = "#1f77b4"
MEDIAN_COLOR = "#ff7f0e"
POINT_COLOR = "#9aa0a6"


def main() -> None:
    instance = instance_from_env(BENCHMARK_INSTANCE_ENV, BENCHMARK_INSTANCE)
    ln.connect(instance)
    ln.track(params={"experiment": NAME})
    try:
        experiment = experiment_record()
        samples = load_samples(require_child(experiment, RUNS_TYPE))
        summary = summarize(samples)
        print(summary.to_string(index=False))
        figure = latency_figure(samples, summary)
        table, plot = upload(summary, figure, instance)
        analysis = record_analysis(experiment, [table, plot], instance=instance)
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


def schema_feature(experiment: ln.Record, name: str) -> ln.Feature:
    feature = experiment.schema.members.filter(name=name).one_or_none()
    if feature is None:
        sys.exit(f"{experiment.name} schema has no {name} column")
    return feature


def load_samples(experiment: ln.Record) -> pd.DataFrame:
    artifact_feature = schema_feature(experiment, "artifact")
    rows: list[dict] = []
    succeeded = 0
    for record in ln.Record.filter(type=experiment, is_type=False):
        values = record.features.get_values()
        if values.get("succeeded") is not True:
            continue
        succeeded += 1
        n_rows = _as_int(values.get("n_rows"), f"{record.name} n_rows")
        artifacts = _measurements(record, artifact_feature)
        if not artifacts:
            print(f"[warn] {record.name} succeeded but has no measurement artifact")
            continue
        for artifact in artifacts:
            frame = artifact.load()
            if not isinstance(frame, pd.DataFrame) or LATENCY not in frame.columns:
                print(f"[warn] {artifact.key or artifact.uid} has no {LATENCY} column")
                continue
            query_name = _query_name(artifact)
            latency = pd.to_numeric(frame[LATENCY], errors="coerce").dropna()
            rows.extend(
                {
                    "run_uid": record.name,
                    "query_name": query_name,
                    "n_rows": n_rows,
                    LATENCY: float(value),
                }
                for value in latency
            )
    if succeeded == 0:
        sys.exit(f"no succeeded runs for {NAME}")
    if not rows:
        sys.exit(f"succeeded {NAME} runs have no {LATENCY} measurements")
    return pd.DataFrame(rows)


def summarize(samples: pd.DataFrame) -> pd.DataFrame:
    """Sample mean, median, and standard deviation of latency, by n_rows."""
    rows = []
    grouped = samples.groupby(["query_name", "n_rows"], sort=True)
    for (query_name, n_rows), group in grouped:
        latency = group[LATENCY]
        std = float(latency.std(ddof=1)) if len(latency) >= 2 else None
        rows.append(
            {
                "query_name": query_name,
                "n_rows": int(n_rows),
                "n_runs": int(group["run_uid"].nunique()),
                "n_observations": int(len(latency)),
                "latency_mean_ms": float(latency.mean()),
                "latency_median_ms": float(latency.median()),
                "latency_std_ms": std,
            }
        )
    return pd.DataFrame(rows)


def latency_figure(samples: pd.DataFrame, summary: pd.DataFrame) -> go.Figure:
    """Mean with sample-std error bars, median, and the underlying repetitions."""
    figure = go.Figure()
    queries = list(summary["query_name"].drop_duplicates())
    several = len(queries) > 1
    for query_name in queries:
        points = samples[samples["query_name"] == query_name]
        stats = summary[summary["query_name"] == query_name]
        prefix = f"{query_name} " if several else ""
        figure.add_trace(
            go.Scatter(
                x=points["n_rows"],
                y=points[LATENCY],
                mode="markers",
                name=f"{prefix}repetitions",
                marker={"color": POINT_COLOR, "size": 8, "opacity": 0.75},
                hovertemplate=(
                    "n_rows=%{x}<br>latency=%{y:.3f} ms<extra>repetition</extra>"
                ),
            )
        )
        std = [
            None if value is None or pd.isna(value) else float(value)
            for value in stats["latency_std_ms"]
        ]
        figure.add_trace(
            go.Scatter(
                x=stats["n_rows"],
                y=stats["latency_mean_ms"],
                error_y={
                    "type": "data",
                    "array": std,
                    "visible": True,
                    "color": MEAN_COLOR,
                },
                mode="lines+markers",
                name=f"{prefix}mean ± std",
                line={"color": MEAN_COLOR, "width": 2},
                marker={"size": 9},
                customdata=stats["latency_std_ms"],
                hovertemplate=(
                    "n_rows=%{x}<br>mean=%{y:.3f} ms<br>std=%{customdata:.3f} ms"
                    "<extra>mean ± std</extra>"
                ),
            )
        )
        figure.add_trace(
            go.Scatter(
                x=stats["n_rows"],
                y=stats["latency_median_ms"],
                mode="lines+markers",
                name=f"{prefix}median",
                line={"color": MEDIAN_COLOR, "width": 2, "dash": "dash"},
                marker={"size": 9, "symbol": "diamond"},
                hovertemplate="n_rows=%{x}<br>median=%{y:.3f} ms<extra>median</extra>",
            )
        )
    tick_rows = list(summary["n_rows"].drop_duplicates())
    figure.update_layout(
        title=f"{NAME} query latency by number of records",
        xaxis_title="Number of records",
        yaxis_title="Latency (ms)",
        template="plotly_white",
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "x": 0},
        margin={"t": 90},
    )
    figure.update_xaxes(
        type="log",
        tickmode="array",
        tickvals=tick_rows,
        ticktext=[str(n_rows) for n_rows in tick_rows],
    )
    return figure


def upload(
    summary: pd.DataFrame, figure: go.Figure, instance: str
) -> tuple[ln.Artifact, ln.Artifact]:
    description = (
        f"Mean, median, and sample standard deviation of {LATENCY} by n_rows "
        f"for succeeded {NAME} runs."
    )
    features = {
        _feature("experiment_name", "str"): NAME,
        _feature("statistic", "str"): "latency_by_n_rows",
    }
    table = ln.Artifact.from_dataframe(
        summary,
        key=TABLE_KEY,
        description=description,
        features=features,
    ).save()
    with tempfile.TemporaryDirectory(prefix="benchmark-analysis-") as raw:
        svg_path = Path(raw) / "latency_by_n_rows.svg"
        figure.write_image(svg_path, format="svg")
        plot = ln.Artifact(
            svg_path,
            key=PLOT_KEY,
            description=(
                f"SVG of mean, median, and standard deviation of {LATENCY} "
                f"by n_rows for succeeded {NAME} runs."
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


def _query_name(artifact: ln.Artifact) -> str:
    value = artifact.features.get_values().get("query_name")
    if isinstance(value, str) and value.strip():
        return value.strip()
    stem = Path(artifact.key or artifact.uid).stem
    return stem or artifact.uid


def _as_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        sys.exit(f"{label} must be an integer, got {value!r}")
    return value


if __name__ == "__main__":
    main()
