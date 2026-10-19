"""Insert latency and relation size for succeeded experiment_004 runs.

One grouped boxplot of rolled-back insert latency, with and without the
partial unique index. One bar chart of the column-size change and the index
after a committed batch. Uses the latest succeeded run at each size.

Usage:
  python experiments/experiment_004/analyze.py
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
    run_uid_of,
)

NAME = Path(__file__).resolve().parent.name
LATENCY = "latency_ms"
REPETITION = "repetition"
INSERTS = ("insert_links", "insert_links_indexed")
LABELS = {"insert_links": "no index", "insert_links_indexed": "unique index"}
COLORS = {"insert_links": "#4c78a8", "insert_links_indexed": "#f58518"}
LATENCY_TABLE = f"benchmarks/analysis/{NAME}/insert_latency.parquet"
LATENCY_PLOT = f"benchmarks/analysis/{NAME}/insert_latency.svg"
SIZE_TABLE = f"benchmarks/analysis/{NAME}/relation_size.parquet"
SIZE_PLOT = f"benchmarks/analysis/{NAME}/relation_size.svg"
TITLE = "ToOne insert cost"
DESCRIPTION = (
    "Latency of RecordRecord inserts before and after the partial unique "
    "index, plus the column and populated-index sizes."
)
NOTES = """# ToOne insert cost

Shows the cost of enforcing one link per record and feature, and the bytes added to `lamindb_recordrecord`.

## What is analyzed

Succeeded runs only. The latest succeeded run at each subject count is used. Each config started from an empty load database. The timed statement is the multi-row `INSERT`, not the rollback. Both arms insert the same links into an empty link table. The column is present in both. Only the partial unique index differs.

## How it is reported

One grouped boxplot. The x axis is the subject count as a category. Each size has a box without the index and a box with it. Every repetition is included, including the cold first call. A second figure uses bars for the byte change from adding `to_one` and for the index after one committed batch of links. That batch is not part of the latency sample. A constant default is often metadata-only, so the column bar can be zero.

## Artifacts

- `benchmarks/analysis/experiment_004/insert_latency.parquet`: one row per timed insert repetition.
- `benchmarks/analysis/experiment_004/insert_latency.svg`: the grouped boxplot.
- `benchmarks/analysis/experiment_004/relation_size.parquet`: one row per subject count, with column bytes, index bytes, and DDL times.
- `benchmarks/analysis/experiment_004/relation_size.svg`: bars for those byte counts.
"""


def main() -> None:
    instance = instance_from_env(BENCHMARK_INSTANCE_ENV, BENCHMARK_INSTANCE)
    ln.connect(instance)
    ln.track(params={"experiment": NAME, "analysis": "to_one"})
    try:
        runs = require_child(experiment_record(), RUNS_TYPE)
        latency, sizes = load_samples(runs)
        print(latency.groupby(["n_rows", "query_name"]).size().to_string())
        print(sizes[["n_rows", "column_bytes", "index_bytes_populated"]].to_string())
        table, plot = upload_latency(latency, latency_figure(latency), instance)
        size_table, size_plot = upload_size(sizes, size_figure(sizes), instance)
        analysis = record_analysis(
            experiment_record(),
            [table, plot, size_table, size_plot],
            instance=instance,
            title=TITLE,
            description=DESCRIPTION,
            notes=NOTES,
        )
        print(f"latency table: https://lamin.ai/{instance}/artifact/{table.uid}")
        print(f"latency plot: https://lamin.ai/{instance}/artifact/{plot.uid}")
        print(f"size table: https://lamin.ai/{instance}/artifact/{size_table.uid}")
        print(f"size plot: https://lamin.ai/{instance}/artifact/{size_plot.uid}")
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


def load_samples(runs: ln.Record) -> tuple[pd.DataFrame, pd.DataFrame]:
    feature = _artifact_feature(runs)
    selected = {record.uid: n_rows for record, _values, n_rows in latest_runs(runs)}
    latency: list[dict] = []
    sizes: list[dict] = []
    for record in ln.Record.filter(type=runs, is_type=False):
        if record.uid not in selected:
            continue
        n_rows = selected[record.uid]
        print(f"using {record.name} for {n_rows} subjects")
        for artifact in measurements(record, feature):
            values = artifact.features.get_values()
            query_name = values.get("query_name")
            if query_name in INSERTS and values.get("measurement_kind") == "insert":
                latency.extend(_latency_rows(run_uid_of(record), n_rows, query_name, artifact))
            elif (
                query_name == "relation_size"
                and values.get("measurement_kind") == "distribution"
            ):
                sizes.append(_size_row(run_uid_of(record), n_rows, values))
    if not latency:
        sys.exit(f"succeeded {NAME} runs have no insert measurements")
    if not sizes:
        sys.exit(f"succeeded {NAME} runs have no relation size measurements")
    return pd.DataFrame(latency), pd.DataFrame(sizes)


def latency_figure(samples: pd.DataFrame) -> go.Figure:
    labels = [str(int(size)) for size in sorted(samples["n_rows"].unique())]
    plot = go.Figure()
    for query_name in INSERTS:
        subset = samples[samples["query_name"] == query_name]
        if subset.empty:
            continue
        plot.add_trace(
            go.Box(
                y=subset[LATENCY],
                x=subset["n_rows"].map(lambda size: str(int(size))),
                name=LABELS[query_name],
                marker_color=COLORS[query_name],
                line_color=COLORS[query_name],
                offsetgroup=query_name,
                boxpoints="all",
                jitter=0.35,
                pointpos=0,
            )
        )
    plot.update_xaxes(
        type="category",
        categoryorder="array",
        categoryarray=labels,
        title_text="Subjects",
    )
    plot.update_yaxes(title_text="Insert latency (ms)")
    plot.update_layout(
        title=TITLE,
        template="plotly_white",
        boxmode="group",
        height=520,
        width=840,
        margin={"t": 60, "b": 60},
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "x": 0},
    )
    return plot


def size_figure(sizes: pd.DataFrame) -> go.Figure:
    labels = [str(int(size)) for size in sorted(sizes["n_rows"].unique())]
    ordered = sizes.set_index("n_rows").loc[[int(label) for label in labels]]
    plot = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.12,
        subplot_titles=("Bytes", "DDL time"),
    )
    plot.add_trace(
        go.Bar(
            x=labels,
            y=ordered["column_bytes"],
            name="column added",
            marker_color="#4c78a8",
        ),
        row=1,
        col=1,
    )
    plot.add_trace(
        go.Bar(
            x=labels,
            y=ordered["index_bytes_populated"],
            name="index after links",
            marker_color="#f58518",
        ),
        row=1,
        col=1,
    )
    plot.add_trace(
        go.Bar(
            x=labels,
            y=ordered["alter_ms"],
            name="add column",
            marker_color="#54a24b",
        ),
        row=2,
        col=1,
    )
    plot.add_trace(
        go.Bar(
            x=labels,
            y=ordered["create_index_ms"],
            name="create index",
            marker_color="#e45756",
        ),
        row=2,
        col=1,
    )
    plot.update_xaxes(type="category", categoryorder="array", categoryarray=labels)
    plot.update_xaxes(title_text="Subjects", row=2, col=1)
    plot.update_yaxes(title_text="Bytes", row=1, col=1)
    plot.update_yaxes(title_text="Milliseconds", row=2, col=1)
    plot.update_layout(
        title="ToOne column and index size",
        template="plotly_white",
        barmode="group",
        height=720,
        width=840,
        margin={"t": 80, "b": 60},
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "x": 0},
    )
    return plot


def upload_latency(samples: pd.DataFrame, plot: go.Figure, instance: str):
    features = {
        _feature("experiment_name", "str"): NAME,
        _feature("statistic", "str"): "to_one_insert",
    }
    table = ln.Artifact.from_dataframe(
        samples,
        key=LATENCY_TABLE,
        description=(
            "Rolled-back RecordRecord insert repetitions for the latest "
            "succeeded run at each subject count, with and without the index."
        ),
        features=features,
    ).save()
    artifact = _svg(plot, LATENCY_PLOT, "Grouped boxplot of ToOne insert latency.", features)
    print(f"uploaded latency table and plot to {instance}")
    return table, artifact


def upload_size(sizes: pd.DataFrame, plot: go.Figure, instance: str):
    features = {
        _feature("experiment_name", "str"): NAME,
        _feature("statistic", "str"): "to_one_size",
    }
    table = ln.Artifact.from_dataframe(
        sizes,
        key=SIZE_TABLE,
        description=(
            "Column byte change, empty and populated index sizes, and DDL "
            "times for the latest succeeded run at each subject count."
        ),
        features=features,
    ).save()
    artifact = _svg(plot, SIZE_PLOT, "Bars for ToOne column and index size.", features)
    print(f"uploaded size table and plot to {instance}")
    return table, artifact


def _svg(plot: go.Figure, key: str, description: str, features: dict) -> ln.Artifact:
    with tempfile.TemporaryDirectory(prefix="benchmark-to-one-") as raw:
        svg_path = Path(raw) / Path(key).name
        plot.write_image(svg_path, format="svg")
        return ln.Artifact(
            svg_path,
            key=key,
            description=description,
            features=features,
        ).save()


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


def _latency_rows(
    run_uid: str, n_rows: int, query_name: str, artifact: ln.Artifact
) -> list[dict]:
    frame = artifact.load()
    if not isinstance(frame, pd.DataFrame):
        return []
    if LATENCY not in frame.columns or REPETITION not in frame.columns:
        return []
    rows = []
    for raw in frame.itertuples(index=False):
        row = raw._asdict()
        latency = pd.to_numeric(row.get(LATENCY), errors="coerce")
        repetition = pd.to_numeric(row.get(REPETITION), errors="coerce")
        if pd.isna(latency) or pd.isna(repetition):
            continue
        rows.append(
            {
                "run_uid": run_uid,
                "query_name": query_name,
                "n_rows": n_rows,
                REPETITION: int(repetition),
                "cold": int(repetition) == 1,
                LATENCY: float(latency),
                "index_present": query_name == "insert_links_indexed",
            }
        )
    return rows


def _size_row(run_uid: str, n_rows: int, values: dict) -> dict:
    return {
        "run_uid": run_uid,
        "n_rows": n_rows,
        "column_bytes": _as_int(values.get("column_bytes"), "column_bytes"),
        "index_bytes": _as_int(values.get("index_bytes"), "index_bytes"),
        "index_bytes_populated": _as_int(
            values.get("index_bytes_populated"), "index_bytes_populated"
        ),
        "heap_bytes_populated": _as_int(
            values.get("heap_bytes_populated"), "heap_bytes_populated"
        ),
        "alter_ms": _as_float(values.get("alter_ms"), "alter_ms"),
        "create_index_ms": _as_float(values.get("create_index_ms"), "create_index_ms"),
    }


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


def _as_float(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        sys.exit(f"{label} must be a number, got {value!r}")
    return float(value)


if __name__ == "__main__":
    main()
