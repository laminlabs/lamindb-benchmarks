"""Bar charts of setup cost for succeeded experiment_002 runs.

The x axis is the configured record count as a category. Each facet is one
setup fact: how long creation took, how many rows the database held, how
much disk it used, and the related write counts. Uses the latest succeeded
run at each size.

Usage:
  python experiments/experiment_002/analyze_setup.py
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
from experiments.experiment_002.workload import matches_analysis_workload

NAME = Path(__file__).resolve().parent.name
# column, panel title, y label, scale applied to the stored number, text format
PANELS = (
    ("insert_seconds", "Setup time", "seconds", 1.0, "{:.2f}"),
    ("insert_records_per_sec", "Insert rate", "records/s", 1.0, "{:.1f}"),
    ("n_total_rows", "Total rows", "rows", 1.0, "{:,.0f}"),
    ("disk_space_bytes", "Database size", "MB", 1e-6, "{:.1f}"),
    ("n_events", "Write events", "events", 1.0, "{:,.0f}"),
    ("n_auxiliary_entries", "Auxiliary entries", "rows", 1.0, "{:,.0f}"),
    ("write_events_per_record", "Write events per record", "events", 1.0, "{:.2f}"),
)
TABLE_KEY = f"benchmarks/analysis/{NAME}/setup_summary.parquet"
PLOT_KEY = f"benchmarks/analysis/{NAME}/setup_summary.svg"
TITLE = "Setup cost by record count"
DESCRIPTION = (
    "How long it took to create the intervention records, and the database "
    "size and row counts that creation left behind."
)
NOTES = """# Setup cost by record count

Summarizes the insert and the database state after it. The load database is wiped between configs, so each size is a fresh setup, not a continuation of the smaller one.

## What is analyzed

Succeeded runs only. The latest succeeded run at each record count is used. Setup time and insert rate come from the timed creation of the intervention records. Total rows, database size, write events, and auxiliary entries come from the server facts captured at the end of that run.

## How it is reported

One bar chart per fact, stacked as vertical facets with a shared categorical x axis. The categories are the configured record counts, evenly spaced. Database size is shown in megabytes. The table keeps the raw byte count.

## Artifacts

- `benchmarks/analysis/experiment_002/setup_summary.parquet`: one row per record count, with setup time, insert rate, total rows, disk bytes, write events, and auxiliary entries.
- `benchmarks/analysis/experiment_002/setup_summary.svg`: the bar-chart facets.
"""


def main() -> None:
    instance = instance_from_env(BENCHMARK_INSTANCE_ENV, BENCHMARK_INSTANCE)
    ln.connect(instance)
    ln.track(params={"experiment": NAME, "analysis": "setup"})
    try:
        experiment = experiment_record()
        summary = load_summary(require_child(experiment, RUNS_TYPE))
        print(summary.to_string(index=False))
        table, plot = upload(summary, setup_figure(summary), instance)
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


def load_summary(runs: ln.Record) -> pd.DataFrame:
    if runs.schema is None:
        sys.exit(f"{runs.name} has no schema")
    feature_col = runs.schema.members.filter(name="artifact").one_or_none()
    if feature_col is None:
        sys.exit(f"{runs.name} schema has no artifact column")
    rows = []
    for record, _values, n_rows in latest_runs(runs):
        print(f"using {record.name} for {n_rows} records")
        facts = run_facts(record, feature_col)
        if "insert_seconds" not in facts and "n_total_rows" not in facts:
            print(f"[warn] {record.name} has no setup facts")
            continue
        rows.append({"run_uid": run_uid_of(record), "n_rows": n_rows, **facts})
    if not rows:
        sys.exit(f"succeeded {NAME} runs have no setup measurements")
    return pd.DataFrame(rows).sort_values("n_rows")


def setup_figure(summary: pd.DataFrame) -> go.Figure:
    """Bar charts with a shared categorical x axis, one facet per setup fact."""
    labels = [str(int(size)) for size in summary["n_rows"]]
    present = [panel for panel in PANELS if panel[0] in summary.columns]
    if not present:
        sys.exit("setup summary has none of the plotted columns")
    figure = make_subplots(
        rows=len(present),
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        subplot_titles=tuple(panel[1] for panel in present),
    )
    for index, (column, _title, ylabel, scale, text_format) in enumerate(present, start=1):
        values = pd.to_numeric(summary[column], errors="coerce") * scale
        figure.add_trace(
            go.Bar(
                x=labels,
                y=values,
                marker_color="#4c78a8",
                text=[text_format.format(value) if pd.notna(value) else "" for value in values],
                textposition="outside",
                cliponaxis=False,
                showlegend=False,
                hovertemplate=f"%{{x}} records<br>%{{y}} {ylabel}<extra></extra>",
            ),
            row=index,
            col=1,
        )
        figure.update_yaxes(title_text=ylabel, row=index, col=1)
    figure.update_xaxes(type="category", categoryorder="array", categoryarray=labels)
    figure.update_xaxes(title_text="Intervention records", row=len(present), col=1)
    figure.update_layout(
        title=TITLE,
        template="plotly_white",
        height=240 * len(present),
        width=760,
        margin={"t": 80, "b": 60},
        bargap=0.35,
    )
    return figure


def upload(
    summary: pd.DataFrame, figure: go.Figure, instance: str
) -> tuple[ln.Artifact, ln.Artifact]:
    features = {
        feature("experiment_name", "str"): NAME,
        feature("statistic", "str"): "setup_summary",
    }
    table = ln.Artifact.from_dataframe(
        summary,
        key=TABLE_KEY,
        description=(
            "Setup time, insert rate, total rows, disk bytes, write events, "
            "and auxiliary entries for the latest succeeded run at each size."
        ),
        features=features,
    ).save()
    with tempfile.TemporaryDirectory(prefix="benchmark-setup-") as raw:
        svg_path = Path(raw) / "setup_summary.svg"
        figure.write_image(svg_path, format="svg")
        plot = ln.Artifact(
            svg_path,
            key=PLOT_KEY,
            description=(
                "Bar charts of setup time, row counts, and database size "
                "across categorical record counts."
            ),
            features=features,
        ).save()
    print(f"uploaded table and plot to {instance}")
    return table, plot


def run_facts(record: ln.Record, feature_col: ln.Feature) -> dict:
    health: dict = {}
    insert: dict = {}
    for artifact in measurements(record, feature_col):
        values = artifact.features.get_values()
        kind = measurement_kind(values, artifact)
        if kind == "health":
            health = picked(values, HEALTH_KEYS)
        elif kind == "insert":
            insert = picked(values, INSERT_KEYS)
    merged = {**insert, **health}
    if "insert_seconds" not in merged and "insert_seconds" in insert:
        merged["insert_seconds"] = insert["insert_seconds"]
    return merged


HEALTH_KEYS = (
    "n_records",
    "n_events",
    "n_auxiliary_entries",
    "n_total_rows",
    "disk_space_bytes",
    "insert_seconds",
    "insert_records_per_sec",
    "write_events_per_record",
)
INSERT_KEYS = (
    "insert_seconds",
    "insert_records_per_sec",
    "n_records",
    "n_events",
    "write_events_per_record",
)


def picked(values: dict, keys: tuple[str, ...]) -> dict:
    row = {}
    for key in keys:
        number = optional_number(values.get(key))
        if number is not None:
            row[key] = number
    return row


def measurement_kind(values: dict, artifact: ln.Artifact) -> str:
    kind = values.get("measurement_kind")
    if isinstance(kind, str) and kind.strip():
        return kind.strip()
    name = query_name_of(artifact, values)
    if name in {"health", "insert"}:
        return name
    return ""


def latest_runs(runs: ln.Record) -> list[tuple[ln.Record, dict, int]]:
    chosen: dict[int, tuple[str, ln.Record, dict]] = {}
    for record in ln.Record.filter(type=runs, is_type=False):
        values = record.features.get_values()
        if values.get("succeeded") is not True:
            continue
        if not matches_analysis_workload((record.run.params or {}) if record.run else {}):
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


def optional_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def as_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        sys.exit(f"{label} must be an integer, got {value!r}")
    return value


if __name__ == "__main__":
    main()
