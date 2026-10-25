"""Compare database execute-call time with time outside execute calls.

Usage: python experiments/record_scalability/analyses/query_time_breakdown.py RUN_RECORD_UID
Python: analyze(run_record) or analyze(run_record_uid)
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import matplotlib
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

from experiments.record_scalability.analyses.common import (
    analysis_run,
    arguments,
    numeric,
    save_analysis,
)
from experiments.record_scalability.analyses.query_latency import load_samples
from experiments.record_scalability.analyses.query_styles import LABELS

NAME = "query_time_breakdown"
TITLE = "Where query time is spent"
DESCRIPTION = "Database driver execute-call time versus the remaining client latency, by query and population."
DB_COLOR = "#3266A8"
OTHER_COLOR = "#E6A157"


def components(samples: pd.DataFrame) -> pd.DataFrame:
    """Compute paired differences for each repetition, before aggregation."""
    data = samples.copy()
    data["sql_execute_ms"] = numeric(data, "sql_execute_ms")
    data["sql_execute_count"] = numeric(data, "sql_execute_count", integral=True)
    difference = data["latency_ms"] - data["sql_execute_ms"]
    if difference.lt(-0.002).any():
        raise ValueError(
            "Database execute time exceeds total latency; timings cannot be decomposed"
        )
    # Both input timers are rounded to milliseconds with three decimal places.
    data["db_rounding_adjusted"] = difference.lt(0)
    data["db_time_ms"] = data["sql_execute_ms"].clip(upper=data["latency_ms"])
    data["non_db_time_ms"] = difference.clip(lower=0)
    return data


def component_summary(group: pd.DataFrame) -> dict:
    values = {"count": len(group)}
    if group.empty:
        return values
    for label, column in (
        ("total", "latency_ms"),
        ("db", "db_time_ms"),
        ("non_db", "non_db_time_ms"),
    ):
        series = group[column]
        values.update(
            {
                f"{label}_mean_ms": float(series.mean()),
                f"{label}_median_ms": float(series.median()),
                f"{label}_p95_ms": float(series.quantile(0.95)),
            }
        )
    total = float(group["latency_ms"].sum())
    values["db_fraction"] = float(group["db_time_ms"].sum()) / total if total else None
    values["non_db_fraction"] = (
        float(group["non_db_time_ms"].sum()) / total if total else None
    )
    values["sql_execute_count_mean"] = float(group["sql_execute_count"].mean())
    values["sql_execute_count_min"] = int(group["sql_execute_count"].min())
    values["sql_execute_count_max"] = int(group["sql_execute_count"].max())
    return values


def summarize(samples: pd.DataFrame) -> pd.DataFrame:
    data = components(samples)
    rows = []
    provenance = [
        "source_run_record_uid",
        "source_run_uid",
        "observation_uid",
        "recorded_at",
        "source_artifact_uid",
    ]
    for (query, size), group in data.groupby(["query_name", "n_rows"], sort=True):
        group = group.sort_values("repetition")
        first = group[group["repetition"].eq(1)]
        warm = group[group["repetition"].gt(1)]
        rows.append(
            {
                "query_name": query,
                "n_rows": int(size),
                **{key: group[key].iloc[0] for key in provenance},
                **component_summary(group),
                **{
                    f"first_{key}": value
                    for key, value in component_summary(first).items()
                },
                **{
                    f"warm_{key}": value
                    for key, value in component_summary(warm).items()
                },
                "rounding_adjustment_count": int(group["db_rounding_adjusted"].sum()),
                "repetitions": group["repetition"].tolist(),
                "total_latencies_ms": group["latency_ms"].tolist(),
                "sql_execute_times_ms": group["sql_execute_ms"].tolist(),
                "db_times_ms": group["db_time_ms"].tolist(),
                "non_db_times_ms": group["non_db_time_ms"].tolist(),
                "sql_execute_counts": group["sql_execute_count"].tolist(),
            }
        )
    return pd.DataFrame(rows)


def breakdown_figure(summary: pd.DataFrame):
    known = list(LABELS)
    queries = sorted(
        summary["query_name"].unique(),
        key=lambda name: (
            name not in LABELS,
            known.index(name) if name in LABELS else name,
        ),
    )
    figure, axes = plt.subplots(
        len(queries),
        2,
        squeeze=False,
        sharex=False,
        sharey=False,
        figsize=(14, 3.6 * len(queries)),
        layout="constrained",
    )
    for index, query in enumerate(queries):
        group = summary[summary["query_name"].eq(query)].sort_values("n_rows")
        positions = list(range(len(group)))
        duration, fraction = axes[index]
        duration.bar(positions, group["db_mean_ms"], color=DB_COLOR)
        duration.bar(
            positions,
            group["non_db_mean_ms"],
            bottom=group["db_mean_ms"],
            color=OTHER_COLOR,
        )
        db_percent = group["db_fraction"].fillna(0) * 100
        other_percent = group["non_db_fraction"].fillna(0) * 100
        fraction.bar(positions, db_percent, color=DB_COLOR)
        fraction.bar(positions, other_percent, bottom=db_percent, color=OTHER_COLOR)
        label = LABELS.get(query, query)
        duration.set(title=f"{label}: mean duration", ylabel="Mean latency (ms)")
        fraction.set(
            title=f"{label}: share of total time", ylabel="Time (%)", ylim=(0, 100)
        )
        for axis in (duration, fraction):
            axis.set_xticks(
                positions, [f"{int(size):,}" for size in group["n_rows"]], rotation=35
            )
            axis.set_xlabel("Intervention records")
            axis.spines[["top", "right"]].set_visible(False)
            axis.set_axisbelow(True)
            axis.grid(axis="y", alpha=0.2)
    figure.suptitle(TITLE, fontsize=16)
    figure.legend(
        handles=[
            Patch(color=DB_COLOR, label="DB execute calls (including network)"),
            Patch(color=OTHER_COLOR, label="Outside execute calls"),
        ],
        loc="outside lower center",
        ncol=2,
        frameon=False,
    )
    return figure


def analyze(run_record):
    """Create the same scoped analysis transform and lineage as other analyses."""
    with analysis_run(run_record, Path(__file__), NAME) as (
        ln,
        instance,
        source,
        observations,
    ):
        summary = summarize(load_samples(ln, source, observations))
        figure = breakdown_figure(summary)
        notes = (
            f"# {TITLE}\n\nSource run record: {source.uid}. "
            "DB time is measured client-side inside driver execute calls, including network waits; "
            "it is not PostgreSQL-only execution time. Outside-execute time is total latency minus "
            "execute time for the same repetition and includes fetching, Python processing and "
            "instrumentation overhead. It is not a CPU-time measurement. "
            "All calls, including the first, contribute to stacked means. Component means add to "
            "mean total latency; medians and p95s are reported independently and must not be stacked. "
            "Percentages are summed component durations divided by summed total duration, not an "
            "average of per-call percentages. First-call and warm-call statistics are separate columns. "
            "Zero-duration groups have undefined fractions. Rounding discrepancies up to 0.002 ms "
            "are clamped and counted; larger inconsistencies fail explicitly."
        )
        try:
            return save_analysis(
                ln, instance, source, NAME, TITLE, DESCRIPTION, notes, summary, figure
            )
        finally:
            plt.close(figure)


def main(argv=None):
    args = arguments(__doc__, argv)
    analyze(args.run_record)


if __name__ == "__main__":
    main()
