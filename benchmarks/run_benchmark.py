"""Connect to the benchmark instance, track parameters, run experiment_script.

Parameters come from an optional config path, BENCHMARK_CONFIG, or ./config.yml.
Every key is passed to ln.track(params=...). The file itself is not uploaded.
The benchmark instance is LAMIN_CURRENT_INSTANCE or laminlabs/lamindb-benchmarks.
experiment_script is a path to a Python file, launched in a separate process,
and is not a run-record column. The child receives BENCHMARK_CONFIG so it reads
the same file. Measurement parquet files it writes are uploaded as outputs of
this run. The artifact key gets this run's uid after the experiment name, then
the artifact is linked to a run record on the experiment frame. Tracking ends
after that process exits.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import lamindb as ln

from benchmarks.config import (
    BENCHMARK_INSTANCE,
    BENCHMARK_INSTANCE_ENV,
    CONFIG_ENV,
    instance_from_env,
    read_config,
)
from benchmarks.experiment import record_run, sync_experiment
from benchmarks.measurements import RESULTS_DIR_ENV, load_measurements


def _feature_dtype(value: object) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    sys.exit(f"unsupported measurement feature value: {value!r}")


def _ensure_feature(name: str, dtype: str) -> ln.Feature:
    feature = ln.Feature.filter(name=name).first()
    if feature is not None:
        return feature
    return ln.Feature(name=name, dtype=dtype).save()


def measurement_artifact_key(key: str, experiment_script: Path, run_uid: str) -> str:
    """Insert the benchmark run uid after the experiment name.

    benchmarks/measurements/experiment_001/record_count.parquet becomes
    benchmarks/measurements/experiment_001/<run_uid>/record_count.parquet.
    """
    if not run_uid or run_uid in {".", ".."} or "/" in run_uid or "\\" in run_uid:
        sys.exit(f"invalid run uid for artifact key: {run_uid!r}")
    parts = Path(key).parts
    try:
        index = parts.index(experiment_script.stem)
    except ValueError:
        index = max(len(parts) - 1, 0)
        parts = (*parts[:index], run_uid, *parts[index:])
        return Path(*parts).as_posix()
    if index + 1 < len(parts) and parts[index + 1] == run_uid:
        return Path(*parts).as_posix()
    parts = (*parts[: index + 1], run_uid, *parts[index + 1 :])
    return Path(*parts).as_posix()


def upload_measurements(
    directory: Path,
    instance: str,
    *,
    experiment_script: Path,
    run_uid: str,
) -> list[ln.Artifact]:
    artifacts = []
    for item in load_measurements(directory):
        features = {
            _ensure_feature(name, _feature_dtype(value)): value
            for name, value in item["features"].items()
        }
        artifact = ln.Artifact.from_dataframe(
            item["path"],
            key=measurement_artifact_key(item["key"], experiment_script, run_uid),
            description=item["description"],
            features=features,
        ).save()
        artifacts.append(artifact)
        print(f"measurement: https://lamin.ai/{instance}/artifact/{artifact.uid}")
    return artifacts


def _experiment_script(params: dict) -> Path:
    raw = params.get("experiment_script")
    if not isinstance(raw, str) or not raw.strip():
        sys.exit("parameters must include experiment_script: path to a Python file")
    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()
    if not path.is_file():
        sys.exit(f"experiment_script not found: {path}")
    return path


def _config_path(argv: list[str] | None = None) -> Path | None:
    parser = argparse.ArgumentParser(description="Run one benchmark config.")
    parser.add_argument(
        "config",
        nargs="?",
        help="Config file. Default: BENCHMARK_CONFIG or ./config.yml.",
    )
    args = parser.parse_args(argv)
    if args.config is None:
        return None
    path = Path(args.config)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def main(argv: list[str] | None = None) -> None:
    config_path = _config_path(argv)
    params = read_config(config_path)
    experiment_script = _experiment_script(params)
    instance = instance_from_env(BENCHMARK_INSTANCE_ENV, BENCHMARK_INSTANCE)

    child_env = os.environ.copy()
    child_env.pop("LAMIN_INITIATED_BY_RUN_UID", None)
    if config_path is not None:
        child_env[CONFIG_ENV] = str(config_path)

    exit_code = 1
    ln.connect(instance)
    ln.track(params=params)
    try:
        run = ln.context.run
        if run is None:
            sys.exit("benchmark run is missing after ln.track()")
        record = sync_experiment(experiment_script, params)
        print(f"experiment: https://lamin.ai/{instance}/record/{record.uid}")
        with tempfile.TemporaryDirectory(prefix="benchmark-results-") as raw_results:
            child_env[RESULTS_DIR_ENV] = raw_results
            completed = subprocess.run(
                [sys.executable, str(experiment_script)],
                check=False,
                env=child_env,
            )
            exit_code = completed.returncode
            # The child can fail in ln.finish() after the parquet is written.
            artifacts = upload_measurements(
                Path(raw_results),
                instance,
                experiment_script=experiment_script,
                run_uid=run.uid,
            )
            run_record = record_run(
                record, params, artifacts, instance=instance, exit_code=exit_code
            )
            print(f"run: https://lamin.ai/{instance}/record/{run_record.uid}")
    finally:
        ln.finish()
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
