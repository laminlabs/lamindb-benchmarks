"""Inspect database and table growth across the observations of one run.

Usage: python experiments/record_scalability/analyses/database_growth.py RUN_RECORD_UID
Python: analyze(run_record) or analyze(run_record_uid)
"""

from __future__ import annotations

import io
import sys
import tempfile
from html import escape
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

NAME = "database_growth"
TITLE = "Database growth across intervention populations"
DESCRIPTION = "Database storage, table row counts and normalized growth, with individual table drill-downs."
MIB = 1024**2


def load_snapshots(ln, source, observations):
    frames = []
    for size, observation, values in observations:
        observation.input_of_runs.add(ln.context.run)
        linked = artifacts(ln, observation)
        tables = [
            item
            for item in linked
            if Path(item.key or "").name == "table_distribution.parquet"
        ]
        health = [
            item for item in linked if Path(item.key or "").name == "health.parquet"
        ]
        if len(tables) != 1 or len(health) > 1:
            raise ValueError(
                f"Observation {observation.uid} needs one table distribution and at most one health table"
            )
        frame = measurement(tables[0])
        if frame.empty or "table_name" not in frame:
            raise ValueError(f"Observation {observation.uid} has no measured tables")
        if (
            not frame["table_name"]
            .map(lambda value: isinstance(value, str) and bool(value.strip()))
            .all()
        ):
            raise ValueError("Table names must be nonempty strings")
        if frame["table_name"].duplicated().any():
            raise ValueError("Table distribution contains duplicate names")
        for column in (
            "table_rows",
            "data_size_bytes",
            "index_size_bytes",
            "total_size_bytes",
        ):
            frame[column] = numeric(frame, column, integral=True)
        if (
            frame["total_size_bytes"]
            < frame["data_size_bytes"] + frame["index_size_bytes"]
        ).any():
            raise ValueError("Total table storage is smaller than its data and indexes")
        frame["bloat_ratio"] = numeric(frame, "bloat_ratio")
        database_size = None
        if health:
            facts = measurement(health[0])
            if len(facts) != 1:
                raise ValueError("Health measurements must have exactly one row")
            if "disk_space_bytes" in facts:
                database_size = int(
                    numeric(facts, "disk_space_bytes", integral=True).iloc[0]
                )
        frame = frame.assign(
            n_rows=size,
            database_size_bytes=database_size,
            source_run_record_uid=source.uid,
            source_run_uid=source.run.uid,
            observation_uid=observation.uid,
            recorded_at=values.get("recorded_at"),
            source_artifact_uid=tables[0].uid,
            health_artifact_uid=health[0].uid if health else None,
        )
        frames.append(frame)
    if not frames:
        raise ValueError("No table distributions are available")
    data = pd.concat(frames, ignore_index=True).sort_values(["table_name", "n_rows"])
    denominator = data["n_rows"].replace(0, float("nan"))
    data["rows_per_intervention"] = data["table_rows"] / denominator
    data["bytes_per_intervention"] = data["total_size_bytes"] / denominator
    data["bytes_per_table_row"] = data["total_size_bytes"] / data["table_rows"].replace(
        0, float("nan")
    )
    data["index_fraction"] = data["index_size_bytes"] / data[
        "total_size_bytes"
    ].replace(0, float("nan"))
    data["other_size_bytes"] = (
        data["total_size_bytes"] - data["data_size_bytes"] - data["index_size_bytes"]
    )
    data["row_change_since_previous"] = data.groupby("table_name")["table_rows"].diff()
    data["byte_change_since_previous"] = data.groupby("table_name")[
        "total_size_bytes"
    ].diff()
    return data.reset_index(drop=True)


def row_fit(group):
    """Least-squares table_rows = k * n_rows + intercept, using measured points."""
    measured = group[["n_rows", "table_rows"]].dropna()
    if measured["n_rows"].nunique() < 2:
        return None
    x = measured["n_rows"].astype(float)
    y = measured["table_rows"].astype(float)
    centered_x = x - x.mean()
    k = float((centered_x * (y - y.mean())).sum() / centered_x.pow(2).sum())
    return k, float(y.mean() - k * x.mean())


def review_table(data):
    """Expose review signals without labelling any growth pattern a defect."""
    rows = []
    largest_population = int(data["n_rows"].max())
    for name, group in data.groupby("table_name", sort=True):
        group = group.sort_values("n_rows")
        first, last = group.iloc[0], group.iloc[-1]
        comparable = len(group) > 1
        fit = row_fit(group)
        factor = None
        row_factor = None
        if comparable and first.total_size_bytes > 0 and first.n_rows > 0:
            factor = (last.total_size_bytes / first.total_size_bytes) / (
                last.n_rows / first.n_rows
            )
        if comparable and first.table_rows > 0 and first.n_rows > 0:
            row_factor = (last.table_rows / first.table_rows) / (
                last.n_rows / first.n_rows
            )
        rows.append(
            {
                "table_name": name,
                "snapshot_count": len(group),
                "first_n_rows": int(first.n_rows),
                "last_n_rows": int(last.n_rows),
                "present_at_last_milestone": int(last.n_rows) == largest_population,
                "latest_table_rows": int(last.table_rows),
                "latest_total_size_bytes": int(last.total_size_bytes),
                "latest_total_mib": float(last.total_size_bytes / MIB),
                "added_rows": int(last.table_rows - first.table_rows)
                if comparable
                else None,
                "added_bytes": int(last.total_size_bytes - first.total_size_bytes)
                if comparable
                else None,
                "storage_growth_vs_population": factor,
                "row_growth_vs_population": row_factor,
                "row_slope_k": fit[0] if fit is not None else None,
                "row_fit_intercept": fit[1] if fit is not None else None,
                "latest_rows_per_intervention": last.rows_per_intervention,
                "latest_bytes_per_intervention": last.bytes_per_intervention,
                "latest_index_fraction": last.index_fraction,
                "latest_dead_to_live_estimate": last.bloat_ratio,
            }
        )
    result = pd.DataFrame(rows)
    return result.sort_values(
        ["added_bytes", "latest_total_size_bytes"], ascending=False, na_position="last"
    ).reset_index(drop=True)


def overview_figure(data, review):
    figure, axes = plt.subplots(2, 2, figsize=(15, 11), layout="constrained")
    populations = data.groupby("n_rows", sort=True)
    tracked = populations["total_size_bytes"].sum()
    database = populations["database_size_bytes"].first()
    axes[0, 0].plot(tracked.index, tracked / MIB, "o-", label="Captured tables")
    if database.notna().any():
        axes[0, 0].plot(database.index, database / MIB, "o-", label="Whole database")
    axes[0, 0].set(
        title="Database size and captured table storage",
        xlabel="Intervention records",
        ylabel="Storage (MiB)",
    )
    axes[0, 0].legend()
    latest = (
        data[data["n_rows"].eq(data["n_rows"].max())]
        .nlargest(12, "total_size_bytes")
        .sort_values("total_size_bytes")
    )
    left = pd.Series(0.0, index=latest.index)
    for column, label in (
        ("data_size_bytes", "Data"),
        ("index_size_bytes", "Indexes"),
        ("other_size_bytes", "Other / TOAST"),
    ):
        width = latest[column] / MIB
        axes[0, 1].barh(latest["table_name"], width, left=left, label=label)
        left = left + width
    axes[0, 1].set(
        title="Largest tables at the last milestone (up to 12)", xlabel="Storage (MiB)"
    )
    axes[0, 1].legend()
    selected = review[review["present_at_last_milestone"]].nlargest(
        8, "latest_table_rows"
    )["table_name"]
    storage_selected = review[review["present_at_last_milestone"]].nlargest(
        8, "latest_total_size_bytes"
    )["table_name"]
    milestones = sorted(data["n_rows"].unique())
    for axis, names, column in (
        (axes[1, 0], selected, "rows_per_intervention"),
        (axes[1, 1], storage_selected, "bytes_per_intervention"),
    ):
        for name in names:
            group = (
                data[data["table_name"].eq(name)]
                .set_index("n_rows")
                .reindex(milestones)
            )
            axis.plot(group.index, group[column], "o-", label=name, markersize=3)
        axis.set_xlabel("Intervention records")
        axis.legend(fontsize=7)
    axes[1, 0].set(
        title="Rows per intervention (up to 8 largest row counts)",
        ylabel="Table rows / intervention",
    )
    axes[1, 1].set(
        title="Storage per intervention (up to 8 largest tables)",
        ylabel="Bytes / intervention",
    )
    for axis in axes.flat:
        axis.grid(alpha=0.2)
        axis.spines[["top", "right"]].set_visible(False)
    figure.suptitle(TITLE, fontsize=16)
    return figure


def table_figure(group):
    figure, axes = plt.subplots(2, 2, figsize=(12, 7), layout="constrained")
    x = group["n_rows"]
    axes[0, 0].plot(x, group["table_rows"], "o-", label="Measured rows")
    fit = row_fit(group)
    if fit is not None:
        k, intercept = fit
        axes[0, 0].plot(
            x,
            k * x + intercept,
            "--",
            color="#E6A157",
            label=f"Linear fit: k = {k:,.4g} rows/intervention",
        )
        axes[0, 0].legend(fontsize=9)
    else:
        axes[0, 0].text(
            0.03,
            0.95,
            "k unavailable: need at least 2 populations",
            transform=axes[0, 0].transAxes,
            va="top",
            fontsize=9,
        )
    axes[0, 0].set(title="Table rows", ylabel="Rows")
    for column, label in (
        ("total_size_bytes", "Total"),
        ("data_size_bytes", "Data"),
        ("index_size_bytes", "Indexes"),
    ):
        axes[0, 1].plot(x, group[column] / MIB, "o-", label=label)
    axes[0, 1].set(title="Storage", ylabel="MiB")
    axes[0, 1].legend()
    axes[1, 0].plot(x, group["rows_per_intervention"], "o-")
    axes[1, 0].set(title="Rows per intervention", ylabel="Rows / intervention")
    axes[1, 1].plot(x, group["bytes_per_intervention"], "o-")
    axes[1, 1].set(title="Storage per intervention", ylabel="Bytes / intervention")
    for axis in axes.flat:
        axis.set_xlabel("Intervention records")
        axis.grid(alpha=0.2)
        axis.spines[["top", "right"]].set_visible(False)
    figure.suptitle(str(group["table_name"].iloc[0]))
    return figure


def svg(figure):
    stream = io.StringIO()
    figure.savefig(stream, format="svg")
    contents = stream.getvalue()
    return contents[contents.index("<svg") :]


def report(data, review, figure):
    parts = [
        '<!doctype html><html lang="en"><meta charset="utf-8"><title>Database growth</title>',
        (
            "<style>body{font:15px/1.6 system-ui;margin:32px auto;max-width:1400px;padding:0 24px;color:#172b45}"
            "svg{max-width:100%;height:auto}table{border-collapse:collapse;font-size:12px}td,th{padding:7px;border-bottom:1px solid #ddd;text-align:right}"
            ".scroll{overflow-x:auto}summary{cursor:pointer;font-size:19px;font-weight:600}details{border-top:1px solid #ddd;padding:16px 0}nav a{margin-right:16px}</style>"
        ),
        f"<h1>{TITLE}</h1>",
        (
            "<p>Each point is a completed population milestone of one source execution. "
            "Whole-database storage comes from pg_database_size; table snapshots cover only "
            "the captured Record-related tables and write-event table, not every database table.</p>"
        ),
        svg(figure),
        (
            "<h2>Tables to inspect</h2><p>Sorted by added storage from the first to the last available "
            "snapshot, then current size. Inspect large absolute costs alongside normalized growth. "
            "storage_growth_vs_population = storage growth factor / population growth factor: "
            "above 1 means storage grew faster. Missing values mean insufficient baseline data. "
            "These signals are not proof of a performance problem. Snapshots may have different "
            "table coverage; snapshot_count and present_at_last_milestone make this visible.</p>"
        ),
        '<div class="scroll">'
        + review.to_html(
            index=False,
            border=0,
            na_rep="—",
            float_format=lambda value: f"{value:,.3f}",
        )
        + "</div>",
        (
            "<p>index_fraction covers the main table's indexes. The dead/live estimate comes "
            "from PostgreSQL statistics and is not a measurement of physical bloat. "
            "TOAST and other relation overhead can explain the difference between total, data and index bytes. "
            "With only one milestone the report shows size, not a growth rate.</p>"
        ),
        (
            "<h2>Individual tables</h2><p>Open a table to view its four plots and exact measurements. "
            "Missing snapshots are not filled with zeros.</p><nav>"
        ),
    ]
    names = review["table_name"].tolist()
    parts.extend(
        f'<a href="#table-{index}">{escape(name)}</a>'
        for index, name in enumerate(names)
    )
    parts.append("</nav>")
    columns = [
        "n_rows",
        "table_rows",
        "data_size_bytes",
        "index_size_bytes",
        "other_size_bytes",
        "total_size_bytes",
        "row_change_since_previous",
        "byte_change_since_previous",
        "rows_per_intervention",
        "bytes_per_intervention",
        "bytes_per_table_row",
        "index_fraction",
        "bloat_ratio",
        "recorded_at",
        "observation_uid",
    ]
    for index, name in enumerate(names):
        group = data[data["table_name"].eq(name)].sort_values("n_rows")
        # Reindex only the plot so missing milestones break lines; retain exact measured rows in the table.
        plotted = (
            group.set_index("n_rows")
            .reindex(sorted(data["n_rows"].unique()))
            .reset_index()
        )
        plotted["table_name"] = name
        detail = table_figure(plotted)
        try:
            parts.extend(
                [
                    f'<details id="table-{index}"><summary>{escape(name)}</summary>',
                    svg(detail),
                    '<div class="scroll">',
                    group[columns]
                    .rename(columns={"bloat_ratio": "dead_to_live_estimate"})
                    .to_html(
                        index=False,
                        border=0,
                        na_rep="—",
                        float_format=lambda value: f"{value:,.3f}",
                    ),
                    "</div></details>",
                ]
            )
        finally:
            plt.close(detail)
    return "\n".join([*parts, "</html>"])


def analyze(run_record):
    with analysis_run(run_record, Path(__file__), NAME) as (
        ln,
        instance,
        source,
        observations,
    ):
        data = load_snapshots(ln, source, observations)
        review = review_table(data)
        figure = overview_figure(data, review)
        notes = (
            f"# {TITLE}\n\nSource run record: {source.uid}. "
            "The main table contains each captured table at each milestone, with row/storage deltas, "
            "per-intervention costs, storage composition and source provenance. "
            "Open tables.html for a storage-growth review and expandable per-table plots and measurements. "
            "table_review.parquet contains the review summary. Database size and captured table totals "
            "have different coverage. PostgreSQL's dead/live estimate is not measured physical bloat."
        )
        try:
            with tempfile.TemporaryDirectory(prefix="database-growth-") as directory:
                html = Path(directory) / "tables.html"
                html.write_text(report(data, review, figure))
                ranking = Path(directory) / "table_review.parquet"
                review.to_parquet(ranking, index=False)
                return save_analysis(
                    ln,
                    instance,
                    source,
                    NAME,
                    TITLE,
                    DESCRIPTION,
                    notes,
                    data,
                    figure,
                    extra_files=[
                        (
                            html,
                            "Expandable per-table plots and measurement tables, plus growth review.",
                        ),
                        (
                            ranking,
                            "Per-table storage and row growth review across observed milestones.",
                        ),
                    ],
                )
        finally:
            plt.close(figure)


def main(argv=None):
    args = arguments(__doc__, argv)
    analyze(args.run_record)


if __name__ == "__main__":
    main()
