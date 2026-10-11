"""Create or update the record for an experiment file, and one record per run.

Re-runs update the same experiment record instead of creating a new one.
Load Benchmarks is the parent type. The experiment record is a record frame
under that parent: description and notes come from the experiment file, and
its columns are the experiment parameters. Each benchmark run is a data record of
that frame, so the frame is the link to the experiment. Result artifacts are
linked from the run record.
"""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import lamindb as ln
from lamindb_setup.core.hashing import hash_string

from benchmarks.config import (
    BENCHMARK_INSTANCE,
    BENCHMARK_INSTANCE_ENV,
    instance_from_env,
    read_config,
)

LOAD_BENCHMARKS = "Load Benchmarks"
RUN_INDEX = "run_uid"
# Runner metadata, not experiment parameters. Kept out of the run sheet even
# if an older config still contains them.
EXCLUDED_COLUMN_KEYS = {"experiment_script", "setup_script", "instance", "load_instance"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create or update the experiment record for a Python file."
    )
    parser.add_argument("experiment", help="Path to the experiment Python file.")
    return parser.parse_args(argv)


def experiment_path(raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()
    if not path.is_file():
        sys.exit(f"experiment file not found: {path}")
    if path.suffix != ".py":
        sys.exit(f"experiment file must be a Python file: {path}")
    return path


def relative_reference(path: Path) -> str:
    try:
        return path.relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def docstring_parts(path: Path) -> tuple[str | None, str | None]:
    try:
        doc = ast.get_docstring(ast.parse(path.read_text()))
    except SyntaxError as error:
        sys.exit(f"could not read docstring from {path}: {error}")
    if not doc:
        return None, None
    first = doc.splitlines()[0].strip()
    return first or None, doc


def record_by_name(name: str, *, type_record: ln.Record | None = None) -> ln.Record | None:
    query = ln.Record.filter(name=name)
    if type_record is not None:
        query = query.filter(type=type_record)
    matches = query.to_list()
    if not matches:
        return None
    if len(matches) > 1:
        sys.exit(f"multiple records named {name!r}")
    return matches[0]


def ensure_type(name: str, *, type_record: ln.Record | None = None) -> ln.Record:
    record = record_by_name(name, type_record=type_record)
    if record is None:
        return ln.Record(name=name, is_type=True, type=type_record).save()
    if not record.is_type:
        sys.exit(f"{name!r} exists but is not a record type")
    if type_record is not None and record.type != type_record:
        record.type = type_record
        record.save()
    return record


def set_notes(record: ln.Record, content: str | None) -> None:
    """Overwrite the record readme. Re-runs do not add another version."""
    content = content or ""
    block = record.ablocks.filter(kind="readme", is_latest=True).one_or_none()
    if block is None:
        if not content:
            return
        block = ln.models.RecordBlock(record=record, content=content, kind="readme").save()
    if block.content == content:
        return
    block.content = content
    block.hash = hash_string(content) if content else None
    block.save()


def feature_dtype(value: object) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    sys.exit(f"config values must be strings, numbers, or booleans, got {value!r}")


def config_columns(params: dict) -> dict[str, object]:
    """Scalar experiment parameters that become columns on each run record."""
    columns = {}
    for key, value in params.items():
        if not isinstance(key, str) or not key.strip():
            sys.exit(f"config key must be a non-empty string, got {key!r}")
        if key == RUN_INDEX:
            sys.exit(f"config key {key!r} is reserved")
        if key in EXCLUDED_COLUMN_KEYS:
            continue
        if len(key) > 150:
            sys.exit(f"config key is too long: {key!r}")
        if value is None:
            continue
        feature_dtype(value)
        columns[key] = value
    if not columns:
        sys.exit("config has no values to record as columns")
    return columns


_PY_DTYPES = {"bool": bool, "int": int, "float": float, "str": str}


def ensure_feature(name: str, dtype: str) -> ln.Feature:
    matches = ln.Feature.filter(name=name, is_type=False).to_list()
    if not matches:
        return ln.Feature(name=name, dtype=_PY_DTYPES[dtype]).save()
    if len(matches) > 1:
        sys.exit(f"multiple features named {name!r}")
    feature = matches[0]
    if feature.dtype_as_str != dtype:
        sys.exit(f"feature {name!r} has dtype {feature.dtype_as_str}, not {dtype}")
    return feature


def ensure_run_index(experiment: ln.Record) -> ln.Feature:
    """One index feature per experiment, so run sheets do not share a schema.

    Schema identity is the set of feature uids. A shared index would make two
    experiments with the same config share one schema, and adding a column to
    one would change the other.
    """
    extra = dict(experiment.extra_data or {})
    stored = extra.get("run_index_feature_uid")
    if isinstance(stored, str):
        feature = ln.Feature.filter(uid=stored).one_or_none()
        if feature is not None:
            if feature.dtype_as_str != "str":
                sys.exit(f"run index feature {feature.uid} is not a string")
            return feature
    description = f"Run uid for {experiment.name}."
    feature = ln.Feature.filter(name=RUN_INDEX, description=description).one_or_none()
    if feature is None:
        feature = ln.Feature(name=RUN_INDEX, dtype=str, description=description).save()
    elif feature.dtype_as_str != "str":
        sys.exit(f"run index feature {feature.uid} is not a string")
    extra["run_index_feature_uid"] = feature.uid
    experiment.extra_data = extra
    experiment.save()
    return feature


def ensure_run_schema(
    experiment: ln.Record, columns: dict[str, object]
) -> tuple[ln.Feature, dict[str, ln.Feature]]:
    """Make the experiment a record frame whose columns are the experiment parameters."""
    index = ensure_run_index(experiment)
    features = {
        name: ensure_feature(name, feature_dtype(value))
        for name, value in columns.items()
    }
    schema = experiment.schema
    if schema is None:
        schema = ln.Schema(
            [feature.with_config(optional=True) for feature in features.values()],
            index=index,
            name=f"{experiment.name} runs",
            description=f"Config columns for {experiment.name} benchmark runs.",
        ).save()
        if ln.Record.filter(schema=schema).exclude(pk=experiment.pk).exists():
            sys.exit(f"schema {schema.uid} is already used by another record")
        experiment.schema = schema
        experiment.save()
    else:
        if schema.index is None or schema.index.uid != index.uid:
            sys.exit(f"{experiment.name} schema is not indexed by its run uid")
        members = list(schema.members.all())
        present = {feature.name for feature in members}
        missing = [feature for name, feature in features.items() if name not in present]
        extra = [
            feature
            for feature in members
            if feature.uid != index.uid and feature.name not in features
        ]
        if missing or extra:
            if ln.Record.filter(schema=schema).exclude(pk=experiment.pk).exists():
                sys.exit(f"schema {schema.uid} is shared; refusing to change columns")
            if extra:
                optional_uids = set(schema.optionals.get_uids())
                stale_optional = [
                    feature for feature in extra if feature.uid in optional_uids
                ]
                if stale_optional:
                    schema.optionals.remove(stale_optional)
                schema.remove(extra)
                _clear_feature_values(experiment, extra)
            if missing:
                schema.add(missing)
    return index, features


def _clear_feature_values(experiment: ln.Record, features: list[ln.Feature]) -> None:
    """Drop removed columns from existing run records. One call per feature.

    remove_values returns early when a record has no value for that feature,
    so a combined call would skip the rest of the list.
    """
    for record in ln.Record.filter(type=experiment, is_type=False):
        for feature in features:
            record.features.remove_values(feature)


def record_run(
    experiment: ln.Record,
    params: dict,
    artifacts: list[ln.Artifact],
    *,
    instance: str,
    exit_code: int | None = None,
) -> ln.Record:
    """Create or update the data record for the active benchmark run.

    The record's type is the experiment frame. Its reference is the Lamin run
    URL. Re-entering the same Lamin run updates that record instead of adding
    another row.
    """
    run = ln.context.run
    if run is None:
        sys.exit("record_run requires an active ln.track() run")
    columns = config_columns(params)
    index, features = ensure_run_schema(experiment, columns)
    values = {index: run.uid, **{features[name]: value for name, value in columns.items()}}
    description = f"Benchmark run of {experiment.name}."
    if exit_code is not None:
        description = f"{description} Exit code {exit_code}."
    reference = f"https://lamin.ai/{instance}/run/{run.uid}"
    record = ln.Record.filter(type=experiment, name=run.uid).one_or_none()
    if record is None:
        record = ln.Record(
            type=experiment,
            description=description,
            reference=reference,
            reference_type="url",
            features=values,
        ).save()
    else:
        changed = False
        if record.description != description:
            record.description = description
            changed = True
        if record.reference != reference or record.reference_type != "url":
            record.reference = reference
            record.reference_type = "url"
            changed = True
        if changed:
            record.save()
        record.features.set_values(values)
    if not experiment.input_of_runs.filter(pk=run.pk).exists():
        experiment.input_of_runs.add(run)
    for artifact in artifacts:
        if ln.models.ArtifactRecord.filter(
            artifact=artifact, record=record, feature=None
        ).exists():
            continue
        ln.models.ArtifactRecord(artifact=artifact, record=record).save()
    return record


def sync_experiment(path: Path, params: dict | None = None) -> ln.Record:
    """Update the experiment frame from the Python file. Caller must be connected."""
    parent = ensure_type(LOAD_BENCHMARKS)
    experiment = ensure_type(path.stem, type_record=parent)
    description, notes = docstring_parts(path)
    reference = relative_reference(path)
    changed = False
    if experiment.description != description:
        experiment.description = description
        changed = True
    if experiment.reference != reference or experiment.reference_type != "path":
        experiment.reference = reference
        experiment.reference_type = "path"
        changed = True
    if changed:
        experiment.save()
    set_notes(experiment, notes)
    if params is not None:
        ensure_run_schema(experiment, config_columns(params))
    return experiment


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    path = experiment_path(args.experiment)
    params = read_config()
    instance = instance_from_env(BENCHMARK_INSTANCE_ENV, BENCHMARK_INSTANCE)
    reference = relative_reference(path)

    ln.connect(instance)
    ln.track(params={"experiment": reference})
    try:
        record = sync_experiment(path, params)
        print(f"experiment: https://lamin.ai/{instance}/record/{record.uid}")
        if record.schema is not None:
            index_name = None if record.schema.index is None else record.schema.index.name
            columns = [
                name
                for name in record.schema.members.values_list("name", flat=True)
                if name != index_name
            ]
            print(f"columns: {', '.join(columns)}")
    finally:
        ln.finish()


if __name__ == "__main__":
    main()
