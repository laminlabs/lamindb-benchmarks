"""Summarize and plot query latency within one benchmark run record.

Usage: python experiments/record_scalability/analyses/query_latency.py RUN_RECORD_UID
Python: analyze(run_record) or analyze(run_record_uid)
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import matplotlib
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

from experiments.record_scalability.analyses.common import (
    analysis_run,
    arguments,
    artifacts,
    measurement,
    numeric,
    save_analysis,
)
from experiments.record_scalability.analyses.query_styles import COLORS, LABELS

NAME = "query_latency"
TITLE = "Query latency by intervention population"
DESCRIPTION = (
    "Latency distributions per query and population within one benchmark execution."
)


def load_samples(ln, source, observations) -> pd.DataFrame:
    frames = []
    for size, observation, values in observations:
        observation.input_of_runs.add(ln.context.run)
        seen = set()
        for artifact in artifacts(ln, observation):
            stem = Path(artifact.key or "").stem
            if Path(artifact.key or "").suffix != ".parquet" or stem in {
                "insert",
                "insert_batches",
                "explain",
                "health",
                "table_distribution",
            }:
                continue
            frame = measurement(artifact)
            if frame.empty or "measurement_kind" not in frame:
                continue
            if not frame["measurement_kind"].eq("lookup").all():
                continue
            if (
                "query_name" not in frame
                or frame["query_name"].isna().any()
                or frame["query_name"].nunique() != 1
            ):
                raise ValueError(
                    f"Query artifact {artifact.uid} must identify exactly one query"
                )
            name = frame["query_name"].iloc[0]
            if not isinstance(name, str) or not name.strip() or name in seen:
                raise ValueError(f"Invalid or duplicate query {name!r} at {size} rows")
            seen.add(name)
            frame["latency_ms"] = numeric(frame, "latency_ms")
            frame["repetition"] = numeric(
                frame, "repetition", integral=True, positive=True
            )
            if frame["repetition"].duplicated().any():
                raise ValueError(f"Duplicate repetitions for {name} at {size} rows")
            if (
                "n_rows" in frame
                and not numeric(frame, "n_rows", integral=True).eq(size).all()
            ):
                raise ValueError(
                    f"Measurement population disagrees with observation {observation.uid}"
                )
            expected = values.get(
                "n_repetitions", (source.run.params or {}).get("n_repetitions")
            )
            repetitions = sorted(frame["repetition"].tolist())
            if repetitions != list(range(1, len(frame) + 1)) or (
                expected is not None and len(frame) != expected
            ):
                raise ValueError(f"Incomplete repetitions for {name} at {size} rows")
            frame = frame.assign(
                n_rows=size,
                source_run_record_uid=source.uid,
                source_run_uid=source.run.uid,
                observation_uid=observation.uid,
                recorded_at=values.get("recorded_at"),
                source_artifact_uid=artifact.uid,
            )
            frames.append(frame)
        if not seen:
            raise ValueError(
                f"Observation {observation.uid} has no query latency tables"
            )
    if not frames:
        raise ValueError("The selected run has no query measurements")
    return pd.concat(frames, ignore_index=True).sort_values(
        ["query_name", "n_rows", "repetition"]
    )


def statistics(latencies: pd.Series) -> dict:
    """Linear quantiles and Tukey's 1.5-IQR adjacent-value whiskers."""
    if latencies.empty:
        return {"count": 0}
    q1, median, q3 = (float(latencies.quantile(q)) for q in (0.25, 0.5, 0.75))
    iqr = q3 - q1
    lower, upper = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    inside = latencies.between(lower, upper)
    outliers = latencies[~inside].tolist()
    return {
        "count": len(latencies),
        "latency_min_ms": float(latencies.min()),
        "latency_max_ms": float(latencies.max()),
        "latency_mean_ms": float(latencies.mean()),
        "latency_std_ms": float(latencies.std(ddof=1)) if len(latencies) > 1 else None,
        "latency_q1_ms": q1,
        "latency_median_ms": median,
        "latency_q3_ms": q3,
        "latency_iqr_ms": iqr,
        **{
            f"latency_p{int(q * 100):02d}_ms": float(latencies.quantile(q))
            for q in (0.01, 0.05, 0.90, 0.95, 0.99)
        },
        "lower_fence_ms": lower,
        "upper_fence_ms": upper,
        "lower_whisker_ms": float(latencies[inside].min()),
        "upper_whisker_ms": float(latencies[inside].max()),
        "outlier_count": len(outliers),
        "outlier_latencies_ms": outliers,
    }


def summarize(samples: pd.DataFrame) -> pd.DataFrame:
    rows = []
    provenance = [
        "source_run_record_uid",
        "source_run_uid",
        "observation_uid",
        "recorded_at",
        "source_artifact_uid",
    ]
    for (query, size), group in samples.groupby(["query_name", "n_rows"], sort=True):
        group = group.sort_values("repetition")
        first = group.loc[group.repetition.eq(1), "latency_ms"]
        warm = group.loc[group.repetition.gt(1), "latency_ms"]
        rows.append(
            {
                "query_name": query,
                "n_rows": int(size),
                **{key: group[key].iloc[0] for key in provenance},
                **statistics(group["latency_ms"]),
                "first_call_latency_ms": float(first.iloc[0])
                if not first.empty
                else None,
                **{f"warm_{key}": value for key, value in statistics(warm).items()},
                "repetitions": group["repetition"].tolist(),
                "latencies_ms": group["latency_ms"].tolist(),
            }
        )
    return pd.DataFrame(rows)


def latency_figure(summary: pd.DataFrame):
    queries = sorted(
        summary["query_name"].unique(),
        key=lambda name: (
            name not in LABELS,
            list(LABELS).index(name) if name in LABELS else name,
        ),
    )
    columns = min(2, len(queries))
    rows = math.ceil(len(queries) / columns)
    figure, axes = plt.subplots(
        rows,
        columns,
        squeeze=False,
        sharex=False,
        sharey=False,
        figsize=(7 * columns, 4 * rows),
        layout="constrained",
    )
    for axis, query in zip(axes.flat, queries, strict=False):
        group = summary[summary.query_name.eq(query)].sort_values("n_rows")
        boxes = [
            {
                "label": f"{int(row.n_rows):,}",
                "med": row.latency_median_ms,
                "q1": row.latency_q1_ms,
                "q3": row.latency_q3_ms,
                "whislo": row.lower_whisker_ms,
                "whishi": row.upper_whisker_ms,
                "mean": row.latency_mean_ms,
                "fliers": row.outlier_latencies_ms,
            }
            for row in group.itertuples(index=False)
        ]
        artists = axis.bxp(
            boxes,
            showmeans=True,
            showfliers=True,
            patch_artist=True,
            meanprops={
                "marker": "^",
                "markerfacecolor": "#2CA02C",
                "markeredgecolor": "#2CA02C",
            },
            medianprops={"color": "#222222", "linewidth": 1.5},
        )
        for box in artists["boxes"]:
            box.set(facecolor=COLORS.get(query, "#3266A8"), alpha=0.45)
        axis.set(
            title=LABELS.get(query, query),
            xlabel="Intervention records",
            ylabel="Latency (ms)",
        )
        axis.tick_params(axis="x", labelrotation=35)
        axis.grid(axis="y", alpha=0.2)
        axis.spines[["top", "right"]].set_visible(False)
    for axis in list(axes.flat)[len(queries) :]:
        axis.set_visible(False)
    figure.suptitle(TITLE, fontsize=16)
    figure.legend(
        handles=[
            Line2D(
                [],
                [],
                marker="^",
                linestyle="none",
                color="#2CA02C",
                label="Mean (all calls)",
            ),
            Line2D([], [], color="#222222", linewidth=1.5, label="Median"),
        ],
        loc="outside lower center",
        ncol=2,
        frameon=False,
    )
    return figure


def analyze(run_record):
    """Create and upload one analysis for a benchmark run Record or its UID."""
    with analysis_run(run_record, Path(__file__), NAME) as (
        ln,
        instance,
        source,
        observations,
    ):
        summary = summarize(load_samples(ln, source, observations))
        figure = latency_figure(summary)
        notes = (
            f"# {TITLE}\n\nSource run record: {source.uid}. "
            "One row per query and population. All repetitions, including the first call, "
            "contribute to the boxes. First-call latency and warm-call statistics are separate columns. "
            "Quantiles use linear interpolation; whiskers end at observations within 1.5 IQR of Q1/Q3. "
            "Standard deviation uses ddof=1 and is absent for one sample. Tail percentiles describe "
            "the observed calls; with few repetitions they are not reliable tail estimates. "
            "Every facet has independent categorical population and latency axes."
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
