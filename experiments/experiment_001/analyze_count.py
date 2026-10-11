"""Check that timed counts match the number of records created.

Usage:
  python experiments/experiment_001/analyze_count.py
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
VALUE = "value"
TABLE_KEY = f"benchmarks/analysis/{NAME}/count_match_by_n_rows.parquet"
PLOT_KEY = f"benchmarks/analysis/{NAME}/count_match_by_n_rows.html"
TITLE = "Counted records match created records"
DESCRIPTION = (
    "Checks that each timed count returns the number of records created, "
    "and reports the latency range."
)
NOTES = """# Counted records match created records

Checks whether the timed count returns the number of records that were created.

## What is analyzed

Succeeded runs only. Each repetition recorded the count it returned and how long that count took. A run matches when every repetition's count equals the number of records created for that run.

## How it is reported

One table row per number of records, with how many runs matched and the minimum and maximum latency. The plot shows that latency range against the number of records.
"""
MIN_COLOR = "#1f77b4"
MAX_COLOR = "#d62728"


def main() -> None:
    instance = instance_from_env(BENCHMARK_INSTANCE_ENV, BENCHMARK_INSTANCE)
    ln.connect(instance)
    ln.track(params={"experiment": NAME, "analysis": "count_match"})
    try:
        experiment = experiment_record()
        samples = load_samples(require_child(experiment, RUNS_TYPE))
        summary = summarize(samples)
        print(summary.to_string(index=False))
        table, plot = upload(summary, latency_range(summary), instance)
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
        succeeded += 1
        n_rows = _as_int(values.get("n_rows"), f"{record.name} n_rows")
        artifacts = _measurements(record, feature)
        if not artifacts:
            print(f"[warn] {record.name} succeeded but has no measurement artifact")
            continue
        for artifact in artifacts:
            frame = artifact.load()
            if not isinstance(frame, pd.DataFrame):
                continue
            if LATENCY not in frame.columns or VALUE not in frame.columns:
                print(f"[warn] {artifact.key or artifact.uid} has no count and latency")
                continue
            query_name = _query_name(artifact)
            for raw in frame.itertuples(index=False):
                row = raw._asdict()
                latency = pd.to_numeric(row.get(LATENCY), errors="coerce")
                counted = pd.to_numeric(row.get(VALUE), errors="coerce")
                if pd.isna(latency) or pd.isna(counted):
                    continue
                rows.append(
                    {
                        "run_uid": record.name,
                        "query_name": query_name,
                        "n_rows": n_rows,
                        LATENCY: float(latency),
                        VALUE: int(counted),
                    }
                )
    if succeeded == 0:
        sys.exit(f"no succeeded runs for {NAME}")
    if not rows:
        sys.exit(f"succeeded {NAME} runs have no count measurements")
    return pd.DataFrame(rows)


def summarize(samples: pd.DataFrame) -> pd.DataFrame:
    """Match count and latency range, by number of records created."""
    rows = []
    grouped = samples.groupby(["query_name", "n_rows"], sort=True)
    for (query_name, n_rows), group in grouped:
        matched = 0
        for _, run in group.groupby("run_uid"):
            if (run[VALUE] == n_rows).all():
                matched += 1
        rows.append(
            {
                "query_name": query_name,
                "n_rows": int(n_rows),
                "n_runs": int(group["run_uid"].nunique()),
                "n_matched": matched,
                "latency_min_ms": float(group[LATENCY].min()),
                "latency_max_ms": float(group[LATENCY].max()),
            }
        )
    return pd.DataFrame(rows)


def latency_range(summary: pd.DataFrame) -> go.Figure:
    figure = go.Figure()
    figure.add_trace(
        go.Scatter(
            x=summary["n_rows"],
            y=summary["latency_min_ms"],
            mode="lines+markers",
            name="minimum",
            line={"color": MIN_COLOR, "width": 2},
        )
    )
    figure.add_trace(
        go.Scatter(
            x=summary["n_rows"],
            y=summary["latency_max_ms"],
            mode="lines+markers",
            name="maximum",
            line={"color": MAX_COLOR, "width": 2},
        )
    )
    tick_rows = list(summary["n_rows"].drop_duplicates())
    figure.update_layout(
        title=TITLE,
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
    features = {
        _feature("experiment_name", "str"): NAME,
        _feature("statistic", "str"): "count_match_by_n_rows",
    }
    table = ln.Artifact.from_dataframe(
        summary,
        key=TABLE_KEY,
        description=DESCRIPTION,
        features=features,
    ).save()
    with tempfile.TemporaryDirectory(prefix="benchmark-analysis-") as raw:
        html_path = Path(raw) / "count_match_by_n_rows.html"
        figure.write_html(html_path, include_plotlyjs=True, full_html=True)
        plot = ln.Artifact(
            html_path,
            key=PLOT_KEY,
            description=f"Minimum and maximum count latency by n_rows for {NAME}.",
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
