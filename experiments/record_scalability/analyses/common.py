"""Shared input selection and output tracking for single-run analyses.

Importing this module does not connect to an instance or start an analysis.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from pathlib import Path

import pandas as pd

from benchmarks.config import (
    BENCHMARK_INSTANCE,
    BENCHMARK_INSTANCE_ENV,
    instance_from_env,
)

NAME = "record_scalability"


def arguments(description: str, argv=None):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "run_record", help="UID of the benchmark run Record, not the Lamin Run UID"
    )
    return parser.parse_args(argv)


def population(value, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer, got {value!r}")
    return value


@contextmanager
def analysis_run(source, script: Path, analysis_name: str):
    """Accept a run Record or its UID; track only when explicitly invoked."""
    instance = instance_from_env(BENCHMARK_INSTANCE_ENV, BENCHMARK_INSTANCE)
    if isinstance(source, str):
        import lamindb_setup

        lamindb_setup.connect(instance)
    import lamindb as ln

    from benchmarks.experiment import record_by_name

    record = ln.Record.get(uid=source) if isinstance(source, str) else source
    if not isinstance(record, ln.Record):
        raise TypeError("Expected a benchmark run Record or its UID")
    frame = record.type
    if (
        record.is_type
        or frame is None
        or frame.name != "runs"
        or frame.type is None
        or frame.type.name != NAME
        or record.run_id is None
    ):
        raise ValueError("Source must be a run record under record_scalability / runs")
    if not isinstance(source, str) and ln.setup.settings.instance.slug != instance:
        raise ValueError(
            f"The supplied Record must belong to the configured tracking instance {instance}"
        )
    observations = record_by_name("observations", type_record=frame.type)
    if observations is None or observations.schema is None:
        raise ValueError("This experiment has no observation sheet")
    link = observations.schema.members.get(name="experiment_run")
    selected = list(
        ln.Record.filter(type=observations, is_type=False).filter(link == record)
    )
    if not selected:
        raise ValueError(f"Run record {record.uid} has no completed observations")
    rows = []
    for observation in selected:
        values = observation.features.get_values()
        rows.append(
            (population(values.get("n_rows"), observation.uid), observation, values)
        )
    rows.sort(key=lambda row: row[0])
    if len({size for size, _, _ in rows}) != len(rows):
        raise ValueError(
            "The selected run has multiple observations at the same population"
        )
    from lamindb.base.uids import base62_12

    key = f"benchmarks/experiments/{frame.type.name}/{record.run.uid}/{analysis_name}"
    transform = ln.Transform.filter(key=key, is_latest=True).first()
    # Keep separate version families even when different source runs use the
    # same analysis script and therefore have identical source hashes.
    stem = transform.uid[:12] if transform is not None else base62_12()
    ln.track(
        stem,
        key=key,
        path=script,
        new_run=True,
        params={
            "experiment": NAME,
            "analysis": analysis_name,
            "source_run_record_uid": record.uid,
            "source_run_uid": record.run.uid,
        },
    )
    try:
        record.input_of_runs.add(ln.context.run)
        yield ln, instance, record, rows
    finally:
        ln.finish()


def artifacts(ln, observation):
    """Use direct observation links, never all artifacts of its creator Run."""
    links = ln.models.RecordArtifact.filter(record=observation).select_related("value")
    return list({link.value.uid: link.value for link in links}.values())


def measurement(artifact) -> pd.DataFrame:
    frame = artifact.load()
    if not isinstance(frame, pd.DataFrame):
        raise TypeError(f"Measurement {artifact.uid} must contain a DataFrame")
    return frame.copy()


def numeric(frame: pd.DataFrame, column: str, *, integral=False, positive=False):
    """Reject corrupt measurements instead of silently dropping repetitions."""
    import numpy as np

    if column not in frame:
        raise ValueError(f"Missing measurement column {column!r}")
    if frame[column].map(lambda value: isinstance(value, bool)).any():
        raise ValueError(f"Invalid boolean values in {column}")
    values = pd.to_numeric(frame[column], errors="raise")
    if values.isna().any() or not np.isfinite(values).all() or (values < 0).any():
        raise ValueError(f"{column} must contain finite nonnegative numbers")
    if integral and (values % 1 != 0).any():
        raise ValueError(f"{column} must contain integers")
    if positive and (values <= 0).any():
        raise ValueError(f"{column} must be positive")
    return values.astype("int64") if integral else values.astype(float)


def save_analysis(
    ln,
    instance,
    source,
    name,
    title,
    description,
    notes,
    table,
    figure,
    *,
    extra_files=(),
):
    """Save one measurement table and SVG/PNG figures under the source record."""
    import tempfile

    from benchmarks.experiment import record_analysis

    key = f"benchmarks/analysis/{NAME}/{source.uid}/{name}"
    outputs = [
        ln.Artifact.from_dataframe(
            table, key=f"{key}.parquet", description=description
        ).save()
    ]
    with tempfile.TemporaryDirectory(prefix=f"{name}-") as directory:
        for extension in ("svg", "png"):
            path = Path(directory) / f"{name}.{extension}"
            figure.savefig(path, format=extension, dpi=180)
            outputs.append(
                ln.Artifact(
                    path, key=f"{key}.{extension}", description=description
                ).save()
            )
    for path, file_description in extra_files:
        constructor = (
            ln.Artifact.from_dataframe if path.suffix == ".parquet" else ln.Artifact
        )
        outputs.append(
            constructor(
                path, key=f"{key}/{path.name}", description=file_description
            ).save()
        )
    links = (
        "\n\n## Runs\n\n"
        f"- [Analysis execution that created these artifacts](https://lamin.ai/{instance}/run/{ln.context.run.uid})\n"
        f"- [Source benchmark run record](https://lamin.ai/{instance}/record/{source.uid})\n"
        f"- [Source benchmark execution](https://lamin.ai/{instance}/run/{source.run.uid})\n"
    )
    record = record_analysis(
        source.type.type,
        outputs,
        instance=instance,
        title=title,
        description=description,
        notes=notes + links,
    )
    for artifact in outputs:
        print(f"artifact: https://lamin.ai/{instance}/artifact/{artifact.uid}")
    print(f"analysis: https://lamin.ai/{instance}/record/{record.uid}")
    return record
