"""Read completed observations, with compatibility for historical run artifacts."""

from __future__ import annotations

from pathlib import Path

import pandas as pd


def measurement_records(runs):
    import lamindb as ln

    from benchmarks.experiment import OBSERVATIONS_TYPE, record_by_name

    observations = record_by_name(OBSERVATIONS_TYPE, type_record=runs.type)
    records, observed_runs = [], set()
    if observations is not None:
        for record in ln.Record.filter(type=observations, is_type=False).select_related(
            "run"
        ):
            if ln.models.RecordArtifact.filter(record=record).exists():
                records.append(record)
                observed_runs.add(record.run_id)
    records.extend(
        record
        for record in ln.Record.filter(type=runs, is_type=False).select_related("run")
        if record.run_id not in observed_runs
        and ln.models.RecordArtifact.filter(record=record).exists()
    )
    return records


def observation_values(record):
    values = record.features.get_values()
    if record.type.name == "observations":
        values = {"succeeded": True, "started_at": record.run.started_at, **values}
    if isinstance(values.get("n_rows"), list) and len(values["n_rows"]) == 1:
        values["n_rows"] = values["n_rows"][0]
    return values


def measurement_values(artifact):
    values = artifact.features.get_values()
    if (
        "/observations/" in (artifact.key or "")
        and Path(artifact.key).suffix == ".parquet"
    ):
        frame = artifact.load()
        if isinstance(frame, pd.DataFrame) and not frame.empty:
            values = {**frame.iloc[0].to_dict(), **values}
    return values
