"""Latest-run Notion summaries and figures; no writes to the Notion workspace."""

from __future__ import annotations

import tempfile
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from benchmarks.config import CONFIG_ENV, read_config
from experiments.experiment_002.workload import workload_parameters
from experiments.experiment_005.parameters import API_VERSION
from experiments.experiment_005.workload import NAME

LOOKUPS = ("uid_lookup", "uid_with_features", "uid_batch_with_features")


def warm_summary(samples: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (query, size), group in samples.groupby(["query_name", "n_rows"]):
        warm = group[group.repetition > 1]
        if warm.empty:
            continue
        row = {
            "query_name": query,
            "n_rows": int(size),
            "run_uid": group.run_uid.iloc[0],
            "latency_first_call_ms": group.loc[
                group.repetition == 1, "latency_ms"
            ].iloc[0],
            "latency_median_ms": warm.latency_ms.median(),
            "latency_p05_ms": warm.latency_ms.quantile(0.05),
            "latency_p95_ms": warm.latency_ms.quantile(0.95),
        }
        for col in (
            "api_request_count",
            "api_request_ms",
            "rate_limit_wait_ms",
            "api_response_bytes",
            "retry_count",
        ):
            row[col] = warm[col].median()
        rows.append(row)
    if not rows:
        raise ValueError("Need at least two query repetitions for warm summaries")
    return pd.DataFrame(rows)


def load_data(ln):
    import os

    from benchmarks.experiment import (
        LOAD_BENCHMARKS,
        RUNS_TYPE,
        record_by_name,
        require_child,
    )

    expected = read_config() if os.environ.get(CONFIG_ENV) else {}
    workload = workload_parameters(expected)
    experiment = record_by_name(NAME, type_record=record_by_name(LOAD_BENCHMARKS))
    if experiment is None:
        raise ValueError("No Notion experiment has been run yet")
    runs = require_child(experiment, RUNS_TYPE)
    chosen = {}
    for record in ln.Record.filter(type=runs, is_type=False).select_related("run"):
        values = record.features.get_values()
        if values.get("succeeded") is not True or not record.run:
            continue
        params = record.run.params or {}
        if params.get("backend") != "notion" or any(
            params.get(k) != v for k, v in workload.items()
        ):
            continue
        if any(
            params.get(key) != expected.get(key, default)
            for key, default in (
                ("notion_api_version", API_VERSION),
                ("notion_requests_per_second", 3),
                ("n_batch", 20),
            )
        ):
            continue
        size = int(params["n_rows"])
        stamp = (str(record.run.started_at), record.run.uid)
        if size not in chosen or stamp > chosen[size][0]:
            chosen[size] = (stamp, record)
    if not chosen:
        raise ValueError(
            "No successful Notion runs match this workload/API version/pacing/batch size"
        )
    samples, snapshots = [], []
    feature = runs.schema.members.get(name="artifact")
    for size, (_stamp, record) in sorted(chosen.items()):
        facts = {"n_rows": size, "run_uid": record.run.uid}
        seen = set()
        for link in ln.models.RecordArtifact.filter(record=record, feature=feature):
            artifact = link.value
            if artifact.uid in seen:
                continue
            seen.add(artifact.uid)
            stem = Path(artifact.key).stem
            if stem not in {*LOOKUPS, "health", "insert"}:
                continue
            # Register every consumed table as an input of this analysis run.
            frame = artifact.load()
            features = artifact.features.get_values()
            if stem in LOOKUPS:
                samples.append(
                    frame.assign(n_rows=size, query_name=stem, run_uid=record.run.uid)
                )
            elif stem == "insert":
                facts.update(
                    {
                        key: value
                        for key, value in features.items()
                        if key
                        in {
                            "insert_seconds",
                            "insert_records_per_sec",
                            "existing_rows",
                            "insert_retry_count",
                            "insert_batch_count",
                        }
                    }
                )
                facts["n_inserted_records"] = int(frame.value.iloc[0])
            else:
                facts.update(
                    {
                        key: value
                        for key, value in features.items()
                        if key
                        in {
                            "n_records",
                            "n_catalog_records",
                            "n_relation_links",
                            "server_metrics_available",
                        }
                    }
                )
        snapshots.append(facts)
        print(f"Using {record.run.uid}: {size} records", flush=True)
    if not samples:
        raise ValueError("No lookup measurements found")
    return experiment, pd.concat(samples, ignore_index=True), pd.DataFrame(snapshots)


def figure(kind: str, samples: pd.DataFrame, snapshot: pd.DataFrame):
    summary = warm_summary(samples)
    sizes = sorted(samples.n_rows.unique())
    labels = [f"{int(size):,}" for size in sizes]
    if kind in {"latency_median_p95", "latency_boxplot"}:
        fig, axes = plt.subplots(3, 1, figsize=(9, 9), layout="constrained")
        for ax, query in zip(axes, LOOKUPS, strict=True):
            if kind == "latency_boxplot":
                groups = [
                    samples.loc[
                        (samples.query_name == query) & (samples.n_rows == size),
                        "latency_ms",
                    ]
                    for size in sizes
                ]
                ax.boxplot(groups, tick_labels=labels)
            else:
                data = summary[summary.query_name == query].sort_values("n_rows")
                ax.errorbar(
                    range(len(data)),
                    data.latency_median_ms,
                    fmt="o",
                    capsize=4,
                    yerr=[
                        data.latency_median_ms - data.latency_p05_ms,
                        data.latency_p95_ms - data.latency_median_ms,
                    ],
                )
                ax.set_xticks(
                    range(len(data)), [f"{int(size):,}" for size in data.n_rows]
                )
            ax.set_title(query)
            ax.set_ylabel("Latency (ms)")
            ax.grid(axis="y", alpha=0.2)
        axes[-1].set_xlabel("Total intervention records")
        return fig
    if kind == "setup_summary":
        fig, axes = plt.subplots(4, 1, figsize=(9, 11), layout="constrained")
        for ax, (column, title) in zip(
            axes,
            (
                ("insert_seconds", "Insertion time for new rows (s)"),
                ("n_inserted_records", "Rows added in this run"),
                ("insert_records_per_sec", "Insert throughput (rows/s)"),
                ("n_relation_links", "Total relation references"),
            ),
            strict=True,
        ):
            ax.bar(labels, snapshot[column])
            ax.set_title(title)
        axes[-1].set_xlabel("Total intervention records")
        return fig
    fig, axes = plt.subplots(4, 2, figsize=(13, 13), layout="constrained")
    for ax, (column, title) in zip(
        axes.flat,
        (
            ("n_records", "Total intervention pages"),
            ("n_relation_links", "Total relation references"),
            ("insert_seconds", "Time for new inserts (s)"),
            ("insert_records_per_sec", "Insert throughput (rows/s)"),
            ("latency_median_ms", "Warm client latency (ms)"),
            ("api_request_ms", "Warm HTTP request time (ms)"),
            ("rate_limit_wait_ms", "Warm pacing/retry wait (ms)"),
            ("api_response_bytes", "Warm response bytes"),
        ),
        strict=True,
    ):
        if column in snapshot.columns:
            ax.plot(snapshot.n_rows, snapshot[column], "o")
        else:
            for query in LOOKUPS:
                data = summary[summary.query_name == query]
                ax.plot(data.n_rows, data[column], "o", label=query)
            ax.legend(fontsize=7)
        ax.set_title(title)
        ax.ticklabel_format(axis="x", style="plain")
        ax.grid(alpha=0.2)
    fig.supxlabel("Total intervention records")
    return fig


def main(kind: str):
    import lamindb as ln

    from benchmarks.config import (
        BENCHMARK_INSTANCE,
        BENCHMARK_INSTANCE_ENV,
        instance_from_env,
    )
    from benchmarks.experiment import record_analysis

    instance = instance_from_env(BENCHMARK_INSTANCE_ENV, BENCHMARK_INSTANCE)
    ln.connect(instance)
    ln.track(params={"experiment": NAME, "analysis": kind, "backend": "notion"})
    try:
        experiment, samples, snapshot = load_data(ln)
        summary = warm_summary(samples)
        tables = {kind: samples if kind == "latency_boxplot" else summary}
        if kind == "setup_summary":
            tables = {kind: snapshot}
        elif kind == "dashboard":
            tables = {"dashboard_snapshot": snapshot, "dashboard_queries": summary}
        artifacts = []
        for name, frame in tables.items():
            artifacts.append(
                ln.Artifact.from_dataframe(
                    frame,
                    key=f"benchmarks/analysis/{NAME}/{name}.parquet",
                    description=f"Notion {name}; latest succeeded run per size and matching workload.",
                ).save()
            )
        plot = figure(kind, samples, snapshot)
        with tempfile.TemporaryDirectory(prefix="notion-analysis-") as raw:
            path = Path(raw) / f"{kind}.svg"
            plot.savefig(path, format="svg")
            plt.close(plot)
            artifacts.append(
                ln.Artifact(
                    path,
                    key=f"benchmarks/analysis/{NAME}/{kind}.svg",
                    description=f"Notion {kind}.",
                ).save()
            )
        record = record_analysis(
            experiment,
            artifacts,
            instance=instance,
            title=f"Notion {kind}",
            notes="First call is not cold-cache. Insertion time covers new rows only. HTTP time includes network waits; pacing/retries are reported separately. Notion SQL plans, disk, cache and write-log statistics are unavailable.",
        )
        print(f"analysis: https://lamin.ai/{instance}/record/{record.uid}")
    finally:
        ln.finish()
