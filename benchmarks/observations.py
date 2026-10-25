"""Filesystem handoff from an experiment to its parent. No database imports."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import yaml

from benchmarks.measurements import MANIFEST_NAME, RESULTS_DIR_ENV, load_measurements


def child_arguments(argv=None):
    parser = argparse.ArgumentParser(
        description="Run an experiment and publish observations."
    )
    parser.add_argument("config", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    args.config = args.config.resolve()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    os.environ["BENCHMARK_CONFIG"] = str(args.config)
    return args


def write_run_information(output: Path, values: dict) -> None:
    """Atomically publish one immutable metadata snapshot for the parent run."""
    validate_features(values)
    output.mkdir(parents=True, exist_ok=True)
    destination = output / "run_info.yml"
    if destination.exists():
        raise FileExistsError("Run information has already been published")
    with tempfile.NamedTemporaryFile(
        mode="w", dir=output, prefix=".run-info-", delete=False
    ) as stream:
        yaml.safe_dump(values, stream, sort_keys=False)
        temporary = Path(stream.name)
    temporary.replace(destination)


def validate_name(name: str) -> str:
    if (
        not isinstance(name, str)
        or not name
        or len(name) > 150
        or name.startswith(".")
        or "/" in name
        or "\\" in name
    ):
        raise ValueError(f"Invalid observation name: {name!r}")
    return name


def validate_features(values: dict) -> dict:
    if not isinstance(values, dict):
        raise TypeError("manifest.yml must contain a mapping")
    reserved = {"artifact", "experiment_run", "observation", "observation_uid"}
    for key, value in values.items():
        if not isinstance(key, str) or not key.strip() or len(key) > 150:
            raise ValueError(f"Invalid manifest feature name: {key!r}")
        if key in reserved:
            raise ValueError(
                f"Manifest feature {key!r} is reserved for observation links"
            )
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"Manifest feature {key!r} must contain finite JSON-compatible values"
            ) from error
    if "recorded_at" in values:
        observation_timestamp(values["recorded_at"])
    return values


def observation_timestamp(value: str) -> datetime:
    """Parse an explicit, timezone-aware observation completion timestamp."""
    if not isinstance(value, str):
        raise TypeError("recorded_at must be an ISO 8601 timestamp with a timezone")
    timestamp = datetime.fromisoformat(value)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("recorded_at must include a timezone")
    return timestamp.astimezone(UTC)


def read_observation(path: Path) -> tuple[dict, str | None, list[Path]]:
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"Observation must be a real directory: {path}")
    files = []
    for item in sorted(path.rglob("*")):
        if item.is_symlink():
            raise ValueError(f"Observation cannot contain symbolic links: {item}")
        if item.is_file():
            files.append(item)
        elif not item.is_dir():
            raise ValueError(f"Unsupported observation entry: {item}")
    manifest = path / "manifest.yml"
    raw = yaml.safe_load(manifest.read_text()) if manifest.exists() else None
    values = validate_features({} if raw is None else raw)
    readme = path / "README.md"
    notes = readme.read_text() if readme.exists() else None
    # Control files supply record metadata, rather than measurement artifacts.
    files = [item for item in files if item not in (manifest, readme)]
    return values, notes, files


def flatten_measurements(directory: Path):
    """Move legacy per-file feature metadata into the measurement tables themselves."""
    for item in load_measurements(directory):
        frame = pd.read_parquet(item["path"])
        for key, value in item["features"].items():
            if key not in frame:
                frame[key] = pd.Series([value] * len(frame), index=frame.index)
        frame.to_parquet(item["path"], index=False)
    (directory / MANIFEST_NAME).unlink(missing_ok=True)


@contextmanager
def observation(
    output: Path,
    name: str,
    features: dict | None = None,
    *,
    timestamped=False,
    timestamp_before: str | None = None,
):
    """Publish on successful context exit by renaming within the output filesystem.

    A failed writer leaves an unpublished folder under .pending for inspection.
    Once published, the child must never modify the directory again.
    """
    validate_name(name)
    features = validate_features(dict(features or {}))
    if timestamp_before is not None and (
        not timestamped or not timestamp_before or timestamp_before not in name
    ):
        raise ValueError(
            "timestamp_before requires timestamped=True and a marker present in the name"
        )

    def completed_name(timestamp):
        if timestamp_before is None:
            return f"{name}_{timestamp}"
        prefix, marker, suffix = name.partition(timestamp_before)
        return f"{prefix}_{timestamp}{marker}{suffix}"

    if timestamped:
        validate_name(completed_name("2000-01-01T00:00:00Z"))
    pending, ready = output / ".pending", output / "observations"
    pending.mkdir(parents=True, exist_ok=True)
    ready.mkdir(parents=True, exist_ok=True)
    destination = ready / name
    if destination.exists():
        raise FileExistsError(f"Observation already published: {name}")
    directory = Path(tempfile.mkdtemp(prefix=f"{name}-", dir=pending))
    previous = os.environ.get(RESULTS_DIR_ENV)
    os.environ[RESULTS_DIR_ENV] = str(directory)
    try:
        yield directory
        flatten_measurements(directory)
        if timestamped:
            timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            features["recorded_at"] = timestamp
            destination = ready / completed_name(timestamp)
            if destination.exists():
                raise FileExistsError(
                    f"Observation already published: {destination.name}"
                )
        if features:
            (directory / "manifest.yml").write_text(
                yaml.safe_dump(features, sort_keys=False)
            )
        read_observation(directory)  # validate complete bundles before publishing
        directory.rename(destination)
    finally:
        if previous is None:
            os.environ.pop(RESULTS_DIR_ENV, None)
        else:
            os.environ[RESULTS_DIR_ENV] = previous
