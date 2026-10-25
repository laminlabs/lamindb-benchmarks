"""Plot insertion latency over contiguous population runs and save a Lamin analysis.

Usage:
    python experiments/record_scalability/analyze_insert_batches.py --runs RUN_UID ...
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import StrMethodFormatter

from benchmarks.config import (
    BENCHMARK_INSTANCE,
    BENCHMARK_INSTANCE_ENV,
    instance_from_env,
)
from experiments.record_scalability.workload import workload_parameters

NAME = "record_scalability"
TITLE = "Insert batch latency as the database grows"
KEY = f"benchmarks/analysis/{NAME}/insert_batch_latency"


def stitch_batches(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Preserve actual population coordinates; never offset independent resets."""
    if not frames or any(frame.empty for frame in frames):
        raise ValueError("Each source run must contain insert batches")
    required = {
        "records_before",
        "records_after",
        "batch_size",
        "value",
        "latency_ms",
        "succeeded",
        "source_run_uid",
        "source_artifact_uid",
    }
    for frame in frames:
        if missing := required - set(frame):
            raise ValueError(f"Missing insert-batch columns: {sorted(missing)}")
    data = pd.concat(frames, ignore_index=True)
    if not data.succeeded.eq(True).all():
        raise ValueError("Only successful insert batches can be combined")
    numeric = ["records_before", "records_after", "batch_size", "value", "latency_ms"]
    for column in numeric:
        values = pd.to_numeric(data[column], errors="raise")
        if not np.isfinite(values).all() or (values < 0).any():
            raise ValueError(f"Invalid {column}: need finite nonnegative numbers")
        if column != "latency_ms" and (values % 1 != 0).any():
            raise ValueError(f"Invalid {column}: need whole record counts")
        data[column] = values
    if data.batch_size.nunique() != 1 or (data.batch_size <= 0).any():
        raise ValueError("Compare equal, positive batch sizes; exclude partial batches")
    if not (
        data.value.eq(data.batch_size)
        & (data.records_after - data.records_before).eq(data.batch_size)
    ).all():
        raise ValueError("Batch sizes and population coordinates disagree")
    data = data.sort_values(["records_before", "records_after"]).reset_index(drop=True)
    if not np.array_equal(
        data.records_before.to_numpy()[1:], data.records_after.to_numpy()[:-1]
    ):
        raise ValueError("Population ranges overlap or have gaps; use contiguous runs")
    return data


def load_batches(ln, run_uids: list[str]) -> pd.DataFrame:
    if len(set(run_uids)) != len(run_uids):
        raise ValueError("Source run UIDs must be unique")
    frames, workload = [], None
    for uid in run_uids:
        run = ln.Run.get(uid)
        if run.finished_at is None:
            raise ValueError(f"Source run {uid} has not finished")
        params = run.params or {}
        source_name = Path(str(params.get("experiment", ""))).name
        if source_name != NAME:
            raise ValueError(f"Source run {uid} is not {NAME}")
        effective = workload_parameters(params)
        if workload is not None and effective != workload:
            raise ValueError("Source runs have different workload parameters")
        workload = effective
        candidates = list(
            ln.Artifact.filter(
                run=run,
                key__endswith="/insert_batches.parquet",
            )
        )
        if not candidates:
            raise ValueError(
                f"Source run {uid} has no completed insert-batch measurements"
            )
        # Each milestone contains cumulative batches. Use the furthest completed
        # observation, never concatenate overlapping snapshots of one execution.
        loaded = [(artifact, artifact.load()) for artifact in candidates]
        nonempty = [(artifact, frame) for artifact, frame in loaded if not frame.empty]
        if not nonempty:
            raise ValueError(f"Source run {uid} inserted no records")
        artifact, frame = max(nonempty, key=lambda pair: pair[1].records_after.max())
        frame = frame.assign(source_run_uid=uid, source_artifact_uid=artifact.uid)
        frame = stitch_batches([frame])
        if not isinstance(params["n_rows"], list) and (
            frame.records_before.iloc[0] != params.get("existing_rows", 0)
            or frame.records_after.iloc[-1] != params["n_rows"]
        ):
            raise ValueError(
                f"Source run {uid} does not cover its configured population"
            )
        frames.append(frame)
        print(f"source: {uid}, {len(frame)} batches, artifact {artifact.uid}")
    return stitch_batches(frames)


def batch_figure(data: pd.DataFrame):
    size = int(data.batch_size.iloc[0])
    fig, ax = plt.subplots(figsize=(11, 5.8), layout="constrained")
    blue = "#3266A8"
    ax.plot(
        data.records_after,
        data.latency_ms,
        color=blue,
        linewidth=1,
        alpha=0.8,
        marker="o",
        markersize=2.8,
        markeredgewidth=0,
    )
    ax.set(
        title=TITLE,
        xlabel="Intervention records after batch",
        ylabel=f"Time per {size:,} inserts (ms)",
    )
    ax.set_title(TITLE, fontsize=15, fontweight="semibold", pad=18)
    ax.xaxis.set_major_formatter(StrMethodFormatter("{x:,.0f}"))
    ax.yaxis.set_major_formatter(StrMethodFormatter("{x:,.0f}"))
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", color="#E3E8EF", linewidth=0.8)
    ax.set_axisbelow(True)
    ax.margins(x=0.025, y=0.3)
    low, high = float(data.records_after.min()), float(data.records_after.max())
    for label, index, color, fill, height in (
        ("Lowest", data.latency_ms.idxmin(), "#167D68", "#E8F5F0", 0.35),
        ("Highest", data.latency_ms.idxmax(), "#8B437B", "#F8EDF5", 0.88),
    ):
        point = data.loc[index]
        fraction = (point.records_after - low) / max(high - low, 1)
        text_x = min(max(fraction, 0.17), 0.83)
        ax.scatter(
            [point.records_after],
            [point.latency_ms],
            s=65,
            color=color,
            edgecolor="white",
            linewidth=1.5,
            zorder=4,
        )
        ax.annotate(
            f"{label}: {point.latency_ms:,.0f} ms\n"
            f"at {int(point.records_after):,} records",
            xy=(point.records_after, point.latency_ms),
            xytext=(text_x, height),
            textcoords="axes fraction",
            ha="center",
            va="center",
            color=color,
            fontsize=10,
            fontweight="medium",
            bbox={
                "boxstyle": "round,pad=0.65,rounding_size=0.35",
                "fc": fill,
                "ec": color,
                "lw": 1,
            },
            arrowprops={
                "arrowstyle": "-",
                "color": color,
                "lw": 1.1,
                "shrinkA": 7,
                "shrinkB": 7,
            },
            zorder=5,
        )
    return fig


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runs", nargs="+", required=True, help="Source Lamin run UIDs"
    )
    args = parser.parse_args(argv)
    import lamindb_setup

    instance = instance_from_env(BENCHMARK_INSTANCE_ENV, BENCHMARK_INSTANCE)
    # Select the tracking instance before model imports to avoid loading the
    # local default load instance and then reloading Django's model classes.
    lamindb_setup.connect(instance)
    import lamindb as ln

    from benchmarks.experiment import LOAD_BENCHMARKS, record_analysis, record_by_name

    ln.track(
        new_run=True,
        params={
            "experiment": NAME,
            "analysis": "insert_batches",
            "source_run_uids": json.dumps(args.runs),
        },
    )
    try:
        parent = record_by_name(LOAD_BENCHMARKS)
        experiment = record_by_name(NAME, type_record=parent) if parent else None
        if experiment is None:
            raise ValueError(f"No {NAME} benchmark experiment found")
        data = load_batches(ln, args.runs)
        artifacts = [
            ln.Artifact.from_dataframe(
                data,
                key=f"{KEY}.parquet",
                description="Contiguous successful insert batches, preserving source run/artifact UIDs and original population coordinates.",
            ).save()
        ]
        fig = batch_figure(data)
        try:
            with tempfile.TemporaryDirectory(prefix="insert-batch-analysis-") as raw:
                for extension in ("svg", "png"):
                    path = Path(raw) / f"insert_batch_latency.{extension}"
                    fig.savefig(path, format=extension, dpi=180)
                    artifacts.append(
                        ln.Artifact(
                            path,
                            key=f"{KEY}.{extension}",
                            description="Batch insertion latency versus intervention population, highlighting the observed minimum and maximum.",
                        ).save()
                    )
        finally:
            plt.close(fig)
        sources = "\n".join(
            f"- [Run {uid}](https://lamin.ai/{instance}/run/{uid})" for uid in args.runs
        )
        notes = (
            f"# {TITLE}\n\n"
            f"{len(data)} successful batches of {int(data.batch_size.iloc[0]):,} interventions, "
            f"from {int(data.records_before.iloc[0]):,} to {int(data.records_after.iloc[-1]):,} records. "
            "Client elapsed time includes row generation, saving, commits and retry waits; "
            "it excludes catalog setup, diagnostic serialization and subsequent ANALYZE. "
            "Bubbles identify the observed minimum and maximum. "
            "Run boundaries are omitted visually; provenance is retained in the table and input lineage.\n\n"
            f"## Source runs\n\n{sources}\n"
        )
        record = record_analysis(
            experiment,
            artifacts,
            instance=instance,
            title=TITLE,
            description="Time per insert batch as the intervention population grows, with minimum and maximum callouts.",
            notes=notes,
        )
        for artifact in artifacts:
            print(f"artifact: https://lamin.ai/{instance}/artifact/{artifact.uid}")
        print(f"analysis: https://lamin.ai/{instance}/record/{record.uid}")
    finally:
        ln.finish()


if __name__ == "__main__":
    main()
