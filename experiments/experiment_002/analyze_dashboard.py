"""Eight-panel dashboard for succeeded experiment_002 runs.

Reads measurements captured during each run. The load database is wiped
between configs, so EXPLAIN ANALYZE, table sizes, and server health are not
recomputed here. The first lookup repetition is the cold call and is left out
of the latency lines.

Usage:
  python experiments/experiment_002/analyze_dashboard.py
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
SYMBOLS = {
    "uid_lookup": "circle",
    "uid_with_features": "square",
    "uid_batch_with_features": "diamond",
}
SCAN_COLOR = {
    "index": "#2ca02c",
    "seq": "#d62728",
    "mixed": "#ff7f0e",
    "unknown": "#7f7f7f",
}
SCAN_SYMBOL = {"index": "circle", "seq": "x", "mixed": "square", "unknown": "diamond"}
TITLE = "Intervention growth dashboard"
DESCRIPTION = (
    "Write growth, auxiliary tables, disk, insert rate, client latency, "
    "database execution time, scan type, and cache hit ratio as intervention "
    "records grow."
)
NOTES = """# Intervention growth dashboard

Brings the scale measurements for this experiment into one view. Each point is the latest succeeded run at that number of intervention records.

## What is analyzed

Succeeded runs only. Insert rate is the timed creation of the intervention records. Write events are entries in the database write log after the run. Auxiliary entries are rows in the record link and record JSON tables. Disk space and cache hit ratio are the load database's own figures at the end of the run, not a shared server total.

Client latency is the warm median of the three uid lookups. The first repetition is the cold call and is not plotted. Error bars span the warm 5th to 95th percentile. Database execution time and scan type come from EXPLAIN ANALYZE, captured once after those timed calls because a later analysis cannot see a wiped database. A lookup is an index scan only when every explained record or link statement used an index and none used a sequential scan. Mixed means the plan used both.

## How it is reported

Eight panels, in archive order: write events and records, auxiliary-table entries, disk space, insert throughput, warm client latency, database execution time, scan type, and cache hit ratio. The x axis is the number of intervention records. Every series is an unconnected scatter. The latency panel is the only one with error bars, because the other panels are one observation per record count.

## Artifacts

- `benchmarks/analysis/experiment_002/dashboard_snapshot.parquet`: one row per record count, with write events, auxiliary entries, disk, cache hit ratio, and insert rate.
- `benchmarks/analysis/experiment_002/dashboard_queries.parquet`: one row per lookup and record count, with warm latency, database execution time, and scan type.
- `benchmarks/analysis/experiment_002/dashboard.svg`: the eight-panel figure.
"""
SNAPSHOT_KEY = f"benchmarks/analysis/{NAME}/dashboard_snapshot.parquet"
QUERY_KEY = f"benchmarks/analysis/{NAME}/dashboard_queries.parquet"
PLOT_KEY = f"benchmarks/analysis/{NAME}/dashboard.svg"


def main() -> None:
    instance = instance_from_env(BENCHMARK_INSTANCE_ENV, BENCHMARK_INSTANCE)
    ln.connect(instance)
    ln.track(params={"experiment": NAME, "analysis": "dashboard"})
    try:
        experiment = experiment_record()
        snapshot, queries, storage = load(require_child(experiment, RUNS_TYPE))
        print("snapshot")
        print(snapshot.to_string(index=False) if not snapshot.empty else "(none)")
        print("queries")
        print(queries.to_string(index=False) if not queries.empty else "(none)")
        figure = dashboard_figure(snapshot, queries, storage)
        artifacts = upload(snapshot, queries, figure, instance)
        analysis = record_analysis(
            experiment,
            artifacts,
            instance=instance,
            title=TITLE,
            description=DESCRIPTION,
            notes=NOTES,
        )
        for artifact in artifacts:
            print(f"artifact: https://lamin.ai/{instance}/artifact/{artifact.uid}")
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


def load(runs: ln.Record) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if runs.schema is None:
        sys.exit(f"{runs.name} has no schema")
    feature = runs.schema.members.filter(name="artifact").one_or_none()
    if feature is None:
        sys.exit(f"{runs.name} schema has no artifact column")
    selected = _latest_runs(runs)
    if not selected:
        sys.exit(f"no succeeded runs for {NAME}")
    snapshots = []
    queries = []
    storage = []
    for record, values, n_rows in selected:
        print(f"using {record.name} for {n_rows} records")
        facts = _run_facts(record, feature, n_rows)
        if facts["health"]:
            snapshots.append({"run_uid": run_uid_of(record), "n_rows": n_rows, **facts["health"]})
        elif facts["insert"]:
            snapshots.append({"run_uid": run_uid_of(record), "n_rows": n_rows, **facts["insert"]})
        else:
            snapshots.append({"run_uid": run_uid_of(record), "n_rows": n_rows})
        queries.extend(facts["queries"])
        storage.extend(facts["storage"])
        if not facts["explained"]:
            print(f"[warn] {record.name} has no EXPLAIN ANALYZE measurement")
    if not queries and all(set(row) <= {"run_uid", "n_rows"} for row in snapshots):
        sys.exit(f"succeeded {NAME} runs have no dashboard measurements")
    return pd.DataFrame(snapshots), pd.DataFrame(queries), pd.DataFrame(storage)


def dashboard_figure(
    snapshot: pd.DataFrame, queries: pd.DataFrame, storage: pd.DataFrame
) -> go.Figure:
    steps = _steps(snapshot, queries, storage)
    ratio = _numeric(snapshot, "write_events_per_record").dropna()
    title = TITLE
    if not ratio.empty:
        title += f"    (~{ratio.mean():.1f} write events per record)"
    figure = make_subplots(
        rows=2,
        cols=4,
        subplot_titles=(
            "Write events and records",
            "Entries in auxiliary tables",
            "Disk space occupied",
            "Insert throughput",
            "Client latency — median and p95",
            "DB execution time",
            "Query plan: scan type",
            "Cache hit ratio",
        ),
        horizontal_spacing=0.08,
        vertical_spacing=0.18,
    )
    _counts(figure, snapshot)
    _auxiliary(figure, storage)
    _marker(figure, snapshot, "disk_space_bytes", 1, 3, scale=1e-6, color="#9467bd")
    _marker(figure, snapshot, "insert_records_per_sec", 1, 4, color="#d62728")
    _latency(figure, queries)
    _execution(figure, queries)
    _scans(figure, queries)
    _marker(figure, snapshot, "cache_hit_ratio", 2, 4, color="#2ca02c")
    figure.update_yaxes(type="log", row=1, col=1)
    figure.update_yaxes(range=[0, 1.02], row=2, col=4)
    figure.update_xaxes(title_text="records written")
    for row, col in ((1, 1), (1, 2), (1, 3), (1, 4), (2, 1), (2, 2), (2, 4)):
        _log_x(figure, steps, row, col)
    _log_x(figure, steps, 2, 3)
    figure.update_layout(
        title=title,
        template="plotly_white",
        height=920,
        width=1500,
        legend={"orientation": "h", "y": -0.08},
        margin={"t": 120, "b": 80},
    )
    return figure


def upload(
    snapshot: pd.DataFrame,
    queries: pd.DataFrame,
    figure: go.Figure,
    instance: str,
) -> list[ln.Artifact]:
    features = {
        _feature("experiment_name", "str"): NAME,
        _feature("statistic", "str"): "dashboard",
    }
    artifacts = []
    if not snapshot.empty:
        artifacts.append(
            ln.Artifact.from_dataframe(
                snapshot,
                key=SNAPSHOT_KEY,
                description=(
                    "One row per record count: write events, auxiliary entries, "
                    "disk, cache hit ratio, and insert rate."
                ),
                features=features,
            ).save()
        )
    if not queries.empty:
        artifacts.append(
            ln.Artifact.from_dataframe(
                queries,
                key=QUERY_KEY,
                description=(
                    "One row per lookup and record count: warm latency, "
                    "database execution time, and scan type."
                ),
                features=features,
            ).save()
        )
    with tempfile.TemporaryDirectory(prefix="benchmark-dashboard-") as raw:
        svg_path = Path(raw) / "dashboard.svg"
        figure.write_image(svg_path, format="svg")
        artifacts.append(
            ln.Artifact(
                svg_path,
                key=PLOT_KEY,
                description=(
                    "Eight-panel scatter dashboard. Latency error bars span "
                    "the warm 5th to 95th percentile."
                ),
                features=features,
            ).save()
        )
    print(f"uploaded dashboard to {instance}")
    return artifacts


def _latest_runs(runs: ln.Record) -> list[tuple[ln.Record, dict, int]]:
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
    return [
        (record, values, n_rows)
        for n_rows, (_stamp, record, values) in sorted(chosen.items())
    ]


def _run_facts(record: ln.Record, feature: ln.Feature, n_rows: int) -> dict:
    health: dict = {}
    insert: dict = {}
    explained = False
    queries = []
    storage = []
    samples: dict[str, list[float]] = {name: [] for name in LOOKUPS}
    plans: dict[str, dict] = {}
    for artifact in _measurements(record, feature):
        values = artifact.features.get_values()
        kind = _kind(values, artifact)
        if kind == "health":
            health = _health_row(values)
        elif kind == "insert":
            insert = _insert_row(values)
        elif kind == "distribution":
            storage.append({"n_rows": n_rows, **_table_row(values, artifact)})
        elif kind == "explain":
            explained = True
            source = _source_query(values, artifact)
            if source in LOOKUPS:
                plans[source] = _plan_row(values)
        elif kind == "lookup":
            _collect_warm(artifact, values, samples)
    for key, value in insert.items():
        health.setdefault(key, value)
    for query_name in LOOKUPS:
        row = {
            "run_uid": run_uid_of(record),
            "n_rows": n_rows,
            "query_name": query_name,
            **plans.get(query_name, {}),
        }
        warm = samples.get(query_name) or []
        if warm:
            series = pd.Series(warm)
            row["latency_median_ms"] = float(series.median())
            row["latency_p05_ms"] = float(series.quantile(0.05))
            row["latency_p95_ms"] = float(series.quantile(0.95))
            row["n_warm"] = int(len(warm))
        if len(row) > 4:
            queries.append(row)
    return {
        "health": health,
        "insert": insert,
        "queries": queries,
        "storage": storage,
        "explained": explained,
    }


def _collect_warm(artifact: ln.Artifact, values: dict, samples: dict[str, list[float]]) -> None:
    query_name = values.get("source_query") or values.get("query_name")
    if query_name not in LOOKUPS:
        return
    frame = artifact.load()
    if not isinstance(frame, pd.DataFrame):
        return
    if "latency_ms" not in frame.columns or "repetition" not in frame.columns:
        return
    for raw in frame.itertuples(index=False):
        row = raw._asdict()
        repetition = pd.to_numeric(row.get("repetition"), errors="coerce")
        latency = pd.to_numeric(row.get("latency_ms"), errors="coerce")
        if pd.isna(repetition) or pd.isna(latency) or int(repetition) <= 1:
            continue
        samples[query_name].append(float(latency))


def _counts(figure: go.Figure, snapshot: pd.DataFrame) -> None:
    if snapshot.empty:
        return
    ordered = snapshot.sort_values("n_rows")
    for column, name, symbol in (
        ("n_records", "records written", "circle"),
        ("n_events", "write events", "square"),
    ):
        if column not in ordered.columns:
            continue
        figure.add_trace(
            go.Scatter(
                x=ordered["n_rows"],
                y=pd.to_numeric(ordered[column], errors="coerce"),
                mode="markers",
                name=name,
                marker={"symbol": symbol, "size": 9},
            ),
            row=1,
            col=1,
        )


def _auxiliary(figure: go.Figure, storage: pd.DataFrame) -> None:
    if storage.empty or "table_kind" not in storage.columns:
        return
    aux = storage[storage["table_kind"].isin(["link", "data"])]
    for table, group in aux.groupby("table_name"):
        ordered = group.sort_values("n_rows")
        figure.add_trace(
            go.Scatter(
                x=ordered["n_rows"],
                y=pd.to_numeric(ordered["table_rows"], errors="coerce"),
                mode="markers",
                marker={"size": 8},
                name=str(table).removeprefix("lamindb_"),
            ),
            row=1,
            col=2,
        )


def _latency(figure: go.Figure, queries: pd.DataFrame) -> None:
    if queries.empty or "latency_median_ms" not in queries.columns:
        return
    for query_name in LOOKUPS:
        group = queries[queries["query_name"] == query_name].sort_values("n_rows")
        if group.empty:
            continue
        label = LABELS[query_name]
        color = COLORS[query_name]
        median = pd.to_numeric(group["latency_median_ms"], errors="coerce")
        figure.add_trace(
            go.Scatter(
                x=group["n_rows"],
                y=median,
                mode="markers",
                name=label,
                marker={"color": color, "size": 9, "symbol": SYMBOLS[query_name]},
                error_y=_error_bar(group, color),
            ),
            row=2,
            col=1,
        )


def _execution(figure: go.Figure, queries: pd.DataFrame) -> None:
    if queries.empty or "execution_time_ms" not in queries.columns:
        return
    for query_name in LOOKUPS:
        group = queries[queries["query_name"] == query_name].sort_values("n_rows")
        if group.empty or group["execution_time_ms"].isna().all():
            continue
        figure.add_trace(
            go.Scatter(
                x=group["n_rows"],
                y=pd.to_numeric(group["execution_time_ms"], errors="coerce"),
                mode="markers",
                name=LABELS[query_name],
                marker={"color": COLORS[query_name], "size": 9},
            ),
            row=2,
            col=2,
        )


def _scans(figure: go.Figure, queries: pd.DataFrame) -> None:
    if queries.empty:
        return
    plotted = False
    for kind, color in SCAN_COLOR.items():
        rows = []
        for query_name in LOOKUPS:
            group = queries[queries["query_name"] == query_name]
            for raw in group.itertuples(index=False):
                item = raw._asdict()
                if _scan_kind(item) != kind:
                    continue
                rows.append({"n_rows": item["n_rows"], "label": LABELS[query_name]})
        if not rows:
            continue
        plotted = True
        frame = pd.DataFrame(rows)
        figure.add_trace(
            go.Scatter(
                x=frame["n_rows"],
                y=frame["label"],
                mode="markers",
                name=kind,
                marker={"color": color, "symbol": SCAN_SYMBOL[kind], "size": 12},
            ),
            row=2,
            col=3,
        )
    if not plotted:
        return


def _marker(
    figure: go.Figure,
    snapshot: pd.DataFrame,
    column: str,
    row: int,
    col: int,
    *,
    scale: float = 1.0,
    color: str,
) -> None:
    if snapshot.empty or column not in snapshot.columns:
        return
    ordered = snapshot.sort_values("n_rows")
    figure.add_trace(
        go.Scatter(
            x=ordered["n_rows"],
            y=pd.to_numeric(ordered[column], errors="coerce") * scale,
            mode="markers",
            name=column,
            marker={"color": color, "size": 9},
            showlegend=False,
        ),
        row=row,
        col=col,
    )


def _error_bar(group: pd.DataFrame, color: str) -> dict | None:
    if "latency_p05_ms" not in group.columns or "latency_p95_ms" not in group.columns:
        return None
    center = pd.to_numeric(group["latency_median_ms"], errors="coerce")
    below = (center - pd.to_numeric(group["latency_p05_ms"], errors="coerce")).clip(lower=0)
    above = (pd.to_numeric(group["latency_p95_ms"], errors="coerce") - center).clip(lower=0)
    return {
        "type": "data",
        "symmetric": False,
        "array": above.tolist(),
        "arrayminus": below.tolist(),
        "color": color,
        "thickness": 1.4,
        "width": 6,
    }


def _log_x(figure: go.Figure, steps: list[int], row: int, col: int) -> None:
    figure.update_xaxes(
        type="log",
        tickmode="array",
        tickvals=steps,
        ticktext=[str(step) for step in steps],
        row=row,
        col=col,
    )


def _steps(*frames: pd.DataFrame) -> list[int]:
    values: set[int] = set()
    for frame in frames:
        if frame.empty or "n_rows" not in frame.columns:
            continue
        for value in pd.to_numeric(frame["n_rows"], errors="coerce").dropna():
            values.add(int(value))
    return sorted(values)


def _health_row(values: dict) -> dict:
    row = {}
    for key in (
        "n_records",
        "n_events",
        "n_auxiliary_entries",
        "n_total_rows",
        "disk_space_bytes",
        "active_connections",
        "cache_hit_ratio",
        "insert_seconds",
        "insert_records_per_sec",
        "write_events_per_record",
    ):
        number = _optional_number(values.get(key))
        if number is not None:
            row[key] = number
    return row


def _insert_row(values: dict) -> dict:
    row = {}
    for key in ("insert_seconds", "insert_records_per_sec", "n_records", "n_events"):
        number = _optional_number(values.get(key))
        if number is not None:
            row[key] = number
    return row


def _table_row(values: dict, artifact: ln.Artifact) -> dict:
    table = values.get("table_name") or _query_name(artifact).removeprefix("table_")
    rows = _optional_number(values.get("table_rows"))
    if rows is None:
        frame = artifact.load()
        if isinstance(frame, pd.DataFrame) and "value" in frame.columns:
            rows = _optional_number(frame["value"].iloc[-1])
    return {
        "table_name": table,
        "table_kind": values.get("table_kind") or "base",
        "table_rows": rows,
    }


def _plan_row(values: dict) -> dict:
    row = {
        "used_index": _truthy(values.get("used_index")),
        "used_seq_scan": _truthy(values.get("used_seq_scan")),
        "scan": _scan_kind(values),
        "plan_source": values.get("plan_source"),
    }
    for key in ("execution_time_ms", "planning_time_ms", "n_statements"):
        number = _optional_number(values.get(key))
        if number is not None:
            row[key] = number
    return row


def _kind(values: dict, artifact: ln.Artifact) -> str:
    kind = values.get("measurement_kind")
    if isinstance(kind, str) and kind.strip():
        return kind.strip()
    name = _query_name(artifact)
    if name in LOOKUPS:
        return "lookup"
    if name.startswith("explain_"):
        return "explain"
    if name in {"health", "insert"}:
        return name
    if name.startswith("table_"):
        return "distribution"
    return ""


def _source_query(values: dict, artifact: ln.Artifact) -> str:
    source = values.get("source_query")
    if isinstance(source, str) and source.strip():
        return source.strip()
    name = _query_name(artifact)
    return name.removeprefix("explain_")


def _query_name(artifact: ln.Artifact) -> str:
    value = artifact.features.get_values().get("query_name")
    if isinstance(value, str) and value.strip():
        return value.strip()
    stem = Path(artifact.key or artifact.uid).stem
    return stem or artifact.uid


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


def _scan_kind(row: dict) -> str:
    if "used_index" not in row and "used_seq_scan" not in row:
        return ""
    seq, idx = _truthy(row.get("used_seq_scan")), _truthy(row.get("used_index"))
    if idx and not seq:
        return "index"
    if seq and not idx:
        return "seq"
    if seq and idx:
        return "mixed"
    return "unknown"


def _truthy(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() == "true"
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return False
    return bool(value)


def _numeric(frame: pd.DataFrame, column: str) -> pd.Series:
    if frame.empty or column not in frame.columns:
        return pd.Series(dtype="float64")
    return pd.to_numeric(frame[column], errors="coerce")


def _optional_number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    number = pd.to_numeric(value, errors="coerce")
    if pd.isna(number):
        return None
    return float(number)


def _as_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        sys.exit(f"{label} must be an integer, got {value!r}")
    return value


def _feature(name: str, dtype: str) -> ln.Feature:
    feature = ln.Feature.filter(name=name, is_type=False).first()
    if feature is None:
        return ln.Feature(name=name, dtype=dtype).save()
    if feature.dtype_as_str != dtype:
        sys.exit(f"feature {name!r} has dtype {feature.dtype_as_str}, not {dtype}")
    return feature


if __name__ == "__main__":
    main()
