"""Create or update the record page for an experiment folder.

An experiment is a folder with a README, setup.py, and query.py. README
frontmatter supplies the description. The README body is the notes. Load
Benchmarks is the parent type. The experiment record is a page under that
parent. Its runs
child is the record frame for benchmark runs. Its analysis child is the
record frame for analysis records, which link the artifacts they produced.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

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
RUNS_TYPE = "runs"
ANALYSIS_TYPE = "analysis"
RUN_INDEX = "run_uid"
ANALYSIS_INDEX = "analysis_uid"
README = "README.md"
SETUP_SCRIPT = "setup.py"
QUERY_SCRIPT = "query.py"
# name, stored dtype, constructor dtype, description
RUN_FEATURES = (
    (
        "started_at",
        "datetime64[ns, UTC]",
        "datetime64[ns, UTC]",
        "Start time of the benchmark run.",
    ),
    ("succeeded", "bool", bool, "Whether the benchmark run exited zero."),
    (
        "artifact",
        "list[cat[Artifact]]",
        list[ln.Artifact],
        "Measurement artifacts linked from the benchmark run.",
    ),
)
# Runner metadata, not experiment parameters. Kept out of the run sheet even
# if an older config still contains them.
EXCLUDED_COLUMN_KEYS = {
    "experiment",
    "experiment_script",
    "setup_script",
    "instance",
    "load_instance",
    "failed",
}
RESERVED_CONFIG_KEYS = {RUN_INDEX, *(name for name, *_rest in RUN_FEATURES)}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create or update the experiment record for a folder."
    )
    parser.add_argument(
        "experiment",
        help="Path to an experiment folder with a README, setup.py, and query.py.",
    )
    return parser.parse_args(argv)


def experiment_path(raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()
    if not path.is_dir():
        sys.exit(f"experiment folder not found: {path}")
    required = (README, SETUP_SCRIPT, QUERY_SCRIPT)
    missing = [name for name in required if not (path / name).is_file()]
    if missing:
        sys.exit(f"experiment folder is missing {', '.join(missing)}: {path}")
    return path


def stage_scripts(folder: Path) -> tuple[Path, Path]:
    """setup.py then query.py. Caller must already have validated the folder."""
    return folder / SETUP_SCRIPT, folder / QUERY_SCRIPT


def relative_reference(path: Path) -> str:
    try:
        return path.relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.as_posix()


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
        if key in RESERVED_CONFIG_KEYS:
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


def ensure_described_feature(
    name: str, dtype_str: str, dtype: object, description: str
) -> ln.Feature:
    feature = ln.Feature.filter(
        name=name, description=description, is_type=False
    ).one_or_none()
    if feature is None:
        if ln.Feature.filter(name=name, is_type=False).exists():
            sys.exit(f"feature {name!r} exists with a different description")
        return ln.Feature(name=name, dtype=dtype, description=description).save()
    if feature.dtype_as_str != dtype_str:
        sys.exit(f"feature {name!r} has dtype {feature.dtype_as_str}, not {dtype_str}")
    return feature


def run_metadata_features() -> dict[str, ln.Feature]:
    return {
        name: ensure_described_feature(name, dtype_str, dtype, description)
        for name, dtype_str, dtype, description in RUN_FEATURES
    }


def ensure_run_index(frame: ln.Record, experiment_name: str) -> ln.Feature:
    """One index feature per experiment, so run sheets do not share a schema.

    Schema identity is the set of feature uids. A shared index would make two
    experiments with the same config share one schema, and adding a column to
    one would change the other.
    """
    extra = dict(frame.extra_data or {})
    stored = extra.get("run_index_feature_uid")
    if isinstance(stored, str):
        feature = ln.Feature.filter(uid=stored).one_or_none()
        if feature is not None:
            if feature.dtype_as_str != "str":
                sys.exit(f"run index feature {feature.uid} is not a string")
            return feature
    description = f"Run uid for {experiment_name}."
    feature = ln.Feature.filter(name=RUN_INDEX, description=description).one_or_none()
    if feature is None:
        feature = ln.Feature(name=RUN_INDEX, dtype=str, description=description).save()
    elif feature.dtype_as_str != "str":
        sys.exit(f"run index feature {feature.uid} is not a string")
    extra["run_index_feature_uid"] = feature.uid
    frame.extra_data = extra
    frame.save()
    return feature


def ensure_run_schema(
    frame: ln.Record, columns: dict[str, object]
) -> tuple[ln.Feature, dict[str, ln.Feature]]:
    """Make the runs frame a sheet whose columns are the experiment parameters."""
    experiment_name = frame.type.name if frame.type is not None else frame.name
    index = ensure_run_index(frame, experiment_name)
    features = {
        name: ensure_feature(name, feature_dtype(value))
        for name, value in columns.items()
    }
    features.update(run_metadata_features())
    schema = frame.schema
    if schema is None:
        schema = ln.Schema(
            [feature.with_config(optional=True) for feature in features.values()],
            index=index,
            name=f"{experiment_name} runs",
            description=f"Config columns for {experiment_name} benchmark runs.",
        ).save()
        if ln.Record.filter(schema=schema).exclude(pk=frame.pk).exists():
            sys.exit(f"schema {schema.uid} is already used by another record")
        frame.schema = schema
        frame.save()
    else:
        if schema.index is None or schema.index.uid != index.uid:
            sys.exit(f"{frame.name} schema is not indexed by its run uid")
        members = list(schema.members.all())
        present = {feature.name for feature in members}
        missing = [feature for name, feature in features.items() if name not in present]
        extra = [
            feature
            for feature in members
            if feature.uid != index.uid and feature.name not in features
        ]
        if missing or extra:
            if ln.Record.filter(schema=schema).exclude(pk=frame.pk).exists():
                sys.exit(f"schema {schema.uid} is shared; refusing to change columns")
            if missing:
                schema.add(missing)
            _migrate_failed_column(frame, members, features["succeeded"])
            if extra:
                optional_uids = set(schema.optionals.get_uids())
                stale_optional = [
                    feature for feature in extra if feature.uid in optional_uids
                ]
                if stale_optional:
                    schema.optionals.remove(stale_optional)
                schema.remove(extra)
                _clear_feature_values(frame, extra)
    return index, features


def _migrate_failed_column(
    experiment: ln.Record, members: list[ln.Feature], succeeded: ln.Feature
) -> None:
    """Copy the old failed flag onto succeeded, inverted, before that column is dropped."""
    failed = next((feature for feature in members if feature.name == "failed"), None)
    if failed is None or failed.uid == succeeded.uid:
        return
    index = None if experiment.schema is None else experiment.schema.index
    for record in ln.Record.filter(type=experiment, is_type=False):
        values = record.features.get_values()
        raw = values.get("failed")
        if raw is None or "succeeded" in values:
            continue
        payload: dict[ln.Feature, object] = {succeeded: not bool(raw)}
        if index is not None:
            payload[index] = record.name
        record.features.add_values(payload)


def _clear_feature_values(experiment: ln.Record, features: list[ln.Feature]) -> None:
    """Drop removed columns from existing run records. One call per feature.

    remove_values returns early when a record has no value for that feature,
    so a combined call would skip the rest of the list.
    """
    for record in ln.Record.filter(type=experiment, is_type=False):
        for feature in features:
            record.features.remove_values(feature)


def _set_description(record: ln.Record, description: str) -> ln.Record:
    if record.description != description:
        record.description = description
        record.save()
    return record


def _detach_legacy_run_schema(experiment: ln.Record) -> None:
    """Move a schema that still sits on the experiment page onto its runs frame.

    Older layouts stored run rows directly on the experiment type. A page can
    hold child types; a frame cannot, so the schema and rows move together.
    """
    if experiment.schema is None:
        return
    schema = experiment.schema
    data_records = list(ln.Record.filter(type=experiment, is_type=False))
    extra = dict(experiment.extra_data or {})
    experiment.schema = None
    experiment.save()
    runs = ensure_type(RUNS_TYPE, type_record=experiment)
    if runs.schema is not None and runs.schema.uid != schema.uid:
        sys.exit(f"{runs.name} already has schema {runs.schema.uid}")
    runs.schema = schema
    runs_extra = dict(runs.extra_data or {})
    stored = extra.get("run_index_feature_uid")
    if isinstance(stored, str):
        runs_extra["run_index_feature_uid"] = stored
    runs.extra_data = runs_extra
    runs.description = f"Benchmark runs of {experiment.name}."
    runs.save()
    for record in data_records:
        record.type = runs
        record.save()


def ensure_runs_frame(experiment: ln.Record) -> ln.Record:
    """The runs sheet under the experiment page."""
    _detach_legacy_run_schema(experiment)
    runs = ensure_type(RUNS_TYPE, type_record=experiment)
    return _set_description(runs, f"Benchmark runs of {experiment.name}.")


def ensure_analysis_index(frame: ln.Record, experiment_name: str) -> ln.Feature:
    """A separate index so the analysis sheet does not share the runs schema."""
    extra = dict(frame.extra_data or {})
    stored = extra.get("analysis_index_feature_uid")
    if isinstance(stored, str):
        feature = ln.Feature.filter(uid=stored).one_or_none()
        if feature is not None:
            if feature.dtype_as_str != "str":
                sys.exit(f"analysis index feature {feature.uid} is not a string")
            return feature
    description = f"Analysis run uid for {experiment_name}."
    feature = ln.Feature.filter(
        name=ANALYSIS_INDEX, description=description, is_type=False
    ).one_or_none()
    if feature is None:
        if ln.Feature.filter(name=ANALYSIS_INDEX, is_type=False).exists():
            sys.exit(f"feature {ANALYSIS_INDEX!r} exists with a different description")
        feature = ln.Feature(
            name=ANALYSIS_INDEX, dtype=str, description=description
        ).save()
    elif feature.dtype_as_str != "str":
        sys.exit(f"analysis index feature {feature.uid} is not a string")
    extra["analysis_index_feature_uid"] = feature.uid
    frame.extra_data = extra
    frame.save()
    return feature


def ensure_analysis_frame(experiment: ln.Record) -> ln.Record:
    """A sheet, not a page: each analysis is one row that links its artifacts."""
    frame = ensure_type(ANALYSIS_TYPE, type_record=experiment)
    frame = _set_description(frame, f"Analyses of {experiment.name}.")
    index = ensure_analysis_index(frame, experiment.name)
    title = ensure_described_feature("title", "str", str, "Title of the analysis.")
    metadata = run_metadata_features()
    members = [title, metadata["started_at"], metadata["artifact"]]
    if frame.schema is None:
        schema = ln.Schema(
            [feature.with_config(optional=True) for feature in members],
            index=index,
            name=f"{experiment.name} analysis",
            description=f"Analysis records for {experiment.name}.",
        ).save()
        if ln.Record.filter(schema=schema).exclude(pk=frame.pk).exists():
            sys.exit(f"schema {schema.uid} is already used by another record")
        frame.schema = schema
        frame.save()
        return frame
    schema = frame.schema
    if schema.index is None or schema.index.uid != index.uid:
        sys.exit(f"{frame.name} schema is not indexed by its analysis uid")
    present = {feature.name for feature in schema.members.all()}
    missing = [feature for feature in members if feature.name not in present]
    if missing:
        if ln.Record.filter(schema=schema).exclude(pk=frame.pk).exists():
            sys.exit(f"schema {schema.uid} is shared; refusing to change columns")
        schema.add(missing)
    return frame


def require_child(experiment: ln.Record, name: str) -> ln.Record:
    child = record_by_name(name, type_record=experiment)
    if child is None:
        sys.exit(f"{experiment.name} has no {name} record type")
    return child


def record_run(
    experiment: ln.Record,
    params: dict,
    artifacts: list[ln.Artifact],
    *,
    instance: str,
    exit_code: int | None = None,
) -> ln.Record:
    """Create or update the data record for the active benchmark run.

    The record's type is the runs frame under the experiment page. Its
    reference is the Lamin run URL. The sheet stores the config, the run start
    time, whether it succeeded, and links to the measurement artifacts.
    Re-entering the same Lamin run updates that record instead of adding
    another row.
    """
    run = ln.context.run
    if run is None:
        sys.exit("record_run requires an active ln.track() run")
    if run.started_at is None:
        sys.exit("benchmark run has no start time")
    runs = ensure_runs_frame(experiment)
    columns = config_columns(params)
    index, features = ensure_run_schema(runs, columns)
    values = {
        index: run.uid,
        **{features[name]: value for name, value in columns.items()},
        features["started_at"]: run.started_at,
        features["succeeded"]: exit_code in (None, 0),
    }
    if artifacts:
        values[features["artifact"]] = artifacts
    description = f"Benchmark run of {experiment.name}."
    if exit_code is not None:
        description = f"{description} Exit code {exit_code}."
    reference = f"https://lamin.ai/{instance}/run/{run.uid}"
    record = ln.Record.filter(type=runs, name=run.uid).one_or_none()
    if record is None:
        record = ln.Record(
            type=runs,
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
    if not runs.input_of_runs.filter(pk=run.pk).exists():
        runs.input_of_runs.add(run)
    _link_run_artifacts(record, features["artifact"], artifacts)
    return record


def record_analysis(
    experiment: ln.Record,
    artifacts: list[ln.Artifact],
    *,
    instance: str,
    title: str | None = None,
    description: str | None = None,
    notes: str | None = None,
) -> ln.Record:
    """Create or update the analysis row for the active run and link its artifacts.

    Title is a sheet column. The row name stays the analysis run uid. Description
    and notes are the record description and readme.
    """
    run = ln.context.run
    if run is None:
        sys.exit("record_analysis requires an active ln.track() run")
    if run.started_at is None:
        sys.exit("analysis run has no start time")
    if not artifacts:
        sys.exit("analysis record requires artifacts")
    if title is not None and not title.strip():
        sys.exit("analysis title must be a non-empty string")
    if description is not None and not description.strip():
        sys.exit("analysis description must be a non-empty string")
    frame = ensure_analysis_frame(experiment)
    if frame.schema is None or frame.schema.index is None:
        sys.exit(f"{frame.name} has no indexed schema")
    index = frame.schema.index
    features = {feature.name: feature for feature in frame.schema.members.all()}
    artifact_feature = features.get("artifact")
    started_at = features.get("started_at")
    title_feature = features.get("title")
    if artifact_feature is None or started_at is None or title_feature is None:
        sys.exit(f"{frame.name} schema is missing title, started_at, or artifact")
    values = {
        index: run.uid,
        started_at: run.started_at,
        artifact_feature: artifacts,
    }
    if title is not None:
        values[title_feature] = title.strip()
    description = (
        description.strip()
        if description is not None
        else f"Analysis of {experiment.name}."
    )
    reference = f"https://lamin.ai/{instance}/run/{run.uid}"
    record = ln.Record.filter(type=frame, name=run.uid).one_or_none()
    if record is None:
        record = ln.Record(
            type=frame,
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
    if not frame.input_of_runs.filter(pk=run.pk).exists():
        frame.input_of_runs.add(run)
    _link_run_artifacts(record, artifact_feature, artifacts)
    if notes is not None:
        set_notes(record, notes)
    return record


def _link_run_artifacts(
    record: ln.Record, feature: ln.Feature, artifacts: list[ln.Artifact]
) -> None:
    """Sheet column plus the record annotation. Both must exist for each file."""
    for artifact in artifacts:
        if not ln.models.RecordArtifact.filter(
            record=record, feature=feature, value=artifact
        ).exists():
            ln.models.RecordArtifact(
                record=record, feature=feature, value=artifact
            ).save()
        if not ln.models.ArtifactRecord.filter(
            artifact=artifact, record=record, feature=None
        ).exists():
            ln.models.ArtifactRecord(artifact=artifact, record=record).save()
        if not ln.models.RecordArtifact.filter(
            record=record, feature=feature, value=artifact
        ).exists():
            sys.exit(f"artifact {artifact.uid} was not linked on run record {record.uid}")


def experiment_docs(folder: Path) -> tuple[str, str]:
    """Description comes from README frontmatter. Notes are the body."""
    path = folder / README
    lines = path.read_text().splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        sys.exit(f"experiment README is missing frontmatter: {path}")
    end = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"),
        None,
    )
    if end is None:
        sys.exit(f"experiment README frontmatter is not closed: {path}")
    try:
        meta = yaml.safe_load("".join(lines[1:end]))
    except yaml.YAMLError as error:
        sys.exit(f"could not read README frontmatter in {path}: {error}")
    if not isinstance(meta, dict):
        sys.exit(f"experiment README frontmatter must be a mapping: {path}")
    description = meta.get("description")
    if not isinstance(description, str) or not description.strip():
        sys.exit(f"experiment README frontmatter needs a description: {path}")
    return description.strip(), "".join(lines[end + 1 :]).lstrip("\n")


def sync_experiment(path: Path, params: dict | None = None) -> ln.Record:
    """Update the experiment page and its runs and analysis frames."""
    parent = ensure_type(LOAD_BENCHMARKS)
    experiment = ensure_type(path.name, type_record=parent)
    description, notes = experiment_docs(path)
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
    runs = ensure_runs_frame(experiment)
    ensure_analysis_frame(experiment)
    if params is not None:
        ensure_run_schema(runs, config_columns(params))
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
        for name in (RUNS_TYPE, ANALYSIS_TYPE):
            child = require_child(record, name)
            kind = "frame" if child.schema is not None else "page"
            print(f"{name}: https://lamin.ai/{instance}/record/{child.uid} ({kind})")
            if child.schema is None:
                continue
            index_name = None if child.schema.index is None else child.schema.index.name
            columns = [
                column
                for column in child.schema.members.values_list("name", flat=True)
                if column != index_name
            ]
            print(f"{name} columns: {', '.join(columns)}")
    finally:
        ln.finish()


if __name__ == "__main__":
    main()
