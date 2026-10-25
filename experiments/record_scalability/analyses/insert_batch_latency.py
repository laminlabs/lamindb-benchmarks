"""Plot cumulative batch insertion measurements from one benchmark run record.

Usage: python experiments/record_scalability/analyses/insert_batch_latency.py RUN_RECORD_UID
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

from experiments.record_scalability.analyses.common import (
    analysis_run,
    arguments,
    artifacts,
    measurement,
    numeric,
    save_analysis,
)

NAME = "insert_batch_latency"
TITLE = "Insert batch latency as population grows"
DESCRIPTION = "Latency of each insert batch from the highest-population observation of one execution."


def load_batches(ln, source, observations) -> pd.DataFrame:
    size, observation, values = max(observations, key=lambda row: row[0])
    observation.input_of_runs.add(ln.context.run)
    candidates = [
        artifact
        for artifact in artifacts(ln, observation)
        if Path(artifact.key or "").name == "insert_batches.parquet"
    ]
    if len(candidates) != 1:
        raise ValueError(
            f"Observation {observation.uid} must link exactly one insert_batches.parquet"
        )
    artifact = candidates[0]
    frame = measurement(artifact)
    if frame.empty:
        raise ValueError(
            "The highest-population observation contains no inserted batches"
        )
    frame["latency_ms"] = numeric(frame, "latency_ms")
    for column in (
        "batch_index",
        "batch_size",
        "records_before",
        "records_after",
        "value",
    ):
        frame[column] = numeric(
            frame,
            column,
            integral=True,
            positive=column in {"batch_index", "batch_size"},
        )
    if "succeeded" not in frame or not frame["succeeded"].eq(True).all():
        raise ValueError(
            "A completed observation must contain successful insertion batches"
        )
    frame = frame.sort_values("batch_index").reset_index(drop=True)
    if frame["batch_index"].tolist() != list(range(1, len(frame) + 1)):
        raise ValueError("Batch indices must form one complete sequence starting at 1")
    if not (
        frame["value"].eq(frame["batch_size"])
        & (frame["records_after"] - frame["records_before"]).eq(frame["batch_size"])
    ).all():
        raise ValueError("Batch sizes and population increments disagree")
    if frame["records_before"].tolist()[1:] != frame["records_after"].tolist()[:-1]:
        raise ValueError("Cumulative batches contain overlapping populations or gaps")
    if int(frame["records_after"].iloc[-1]) != size:
        raise ValueError(
            "Insertion history does not reach the selected observation's population"
        )
    starting = values.get("starting_rows")
    if starting is not None and int(frame["records_before"].iloc[0]) != starting:
        raise ValueError(
            "Insertion history does not begin at the run's starting population"
        )
    return frame.assign(
        source_run_record_uid=source.uid,
        source_run_uid=source.run.uid,
        observation_uid=observation.uid,
        recorded_at=values.get("recorded_at"),
        source_artifact_uid=artifact.uid,
        observation_n_rows=size,
    )


def batch_figure(data: pd.DataFrame):
    figure, axis = plt.subplots(figsize=(11, 5), layout="constrained")
    axis.plot(
        data["records_after"],
        data["latency_ms"],
        color="#3266A8",
        linewidth=1,
        marker="o",
        markersize=3,
    )
    axis.set(
        title=TITLE,
        xlabel="Intervention records after batch",
        ylabel="Batch latency (ms)",
    )
    axis.grid(axis="y", alpha=0.2)
    axis.spines[["top", "right"]].set_visible(False)
    return figure


def analyze(run_record):
    """Analyze only the furthest completed observation; never concatenate snapshots."""
    with analysis_run(run_record, Path(__file__), NAME) as (
        ln,
        instance,
        source,
        observations,
    ):
        data = load_batches(ln, source, observations)
        figure = batch_figure(data)
        notes = (
            f"# {TITLE}\n\nSource run record: {source.uid}. "
            f"Uses only observation {data.observation_uid.iloc[0]}, at "
            f"{int(data.observation_n_rows.iloc[0]):,} interventions. "
            "Its insert table contains every batch measured since this execution started; earlier "
            "overlapping snapshots are not loaded. Partial batches are retained with their actual sizes. "
            "The x-axis is intervention population immediately after each batch. "
            "Batch latency includes generation, saving, commits and retry waits; it excludes "
            "queries, maintenance and pauses between milestones. "
            "The y-axis is latency in milliseconds for that batch."
        )
        try:
            return save_analysis(
                ln, instance, source, NAME, TITLE, DESCRIPTION, notes, data, figure
            )
        finally:
            plt.close(figure)


def main(argv=None):
    args = arguments(__doc__, argv)
    analyze(args.run_record)


if __name__ == "__main__":
    main()
