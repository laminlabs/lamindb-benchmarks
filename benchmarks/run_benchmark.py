"""Connect to the benchmark instance, track parameters, and run an experiment.

Parameters come from an optional config path, BENCHMARK_CONFIG, or ./config.yml.
An experiment's optional parameters.py resolves defaults before every key is
passed to ln.track(params=...). The config file itself is not uploaded.
The benchmark instance is LAMIN_CURRENT_INSTANCE or laminlabs/lamindb-benchmarks.
experiment is a folder containing setup.py and query.py. They are launched in
separate processes, setup first, and are not run-record columns. Each child
receives BENCHMARK_CONFIG so it reads the same file. Measurement parquet files
are uploaded as outputs of this run. The artifact key gets this run's uid after
the experiment name, then the artifact is linked to a run record on the
experiment frame. Tracking ends after those processes exit.
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
    LOAD_INSTANCE,
    LOAD_INSTANCE_ENV,
    instance_from_env,
    read_config,
    resolve_parameters,
    run_name,
)
from benchmarks.experiment import (
    experiment_path,
    record_run,
    stage_scripts,
    sync_experiment,
)
from benchmarks.measurements import RESULTS_DIR_ENV, load_measurements
from benchmarks.provenance import attach_git_provenance, benchmark_provenance


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


def measurement_artifact_key(key: str, experiment: Path, run_uid: str) -> str:
    """Insert the benchmark run uid after the experiment folder name.

    benchmarks/measurements/<experiment>/<query>.parquet becomes
    benchmarks/measurements/<experiment>/<run_uid>/<query>.parquet.
    """
    if not run_uid or run_uid in {".", ".."} or "/" in run_uid or "\\" in run_uid:
        sys.exit(f"invalid run uid for artifact key: {run_uid!r}")
    parts = Path(key).parts
    try:
        index = parts.index(experiment.name)
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
    experiment: Path,
    run_uid: str,
) -> list[ln.Artifact]:
    run = ln.context.run
    if run is None or run.uid != run_uid:
        sys.exit("upload_measurements requires the active benchmark run")
    artifacts = []
    for item in load_measurements(directory):
        features = {
            _ensure_feature(name, _feature_dtype(value)): value
            for name, value in item["features"].items()
        }
        artifact = ln.Artifact.from_dataframe(
            item["path"],
            key=measurement_artifact_key(item["key"], experiment, run_uid),
            description=item["description"],
            features=features,
            run=run,
            skip_hash_lookup=True,
        ).save()
        artifacts.append(artifact)
        print(f"measurement: https://lamin.ai/{instance}/artifact/{artifact.uid}")
    return artifacts


def _experiment(params: dict) -> Path:
    raw = params.get("experiment")
    if not isinstance(raw, str) or not raw.strip():
        sys.exit("parameters must include experiment: path to an experiment folder")
    return experiment_path(raw)


def _config_path(argv: list[str] | None = None) -> Path:
    parser = argparse.ArgumentParser(description="Run one benchmark config.")
    parser.add_argument(
        "config",
        nargs="?",
        help="Config file. Default: BENCHMARK_CONFIG or ./config.yml.",
    )
    args = parser.parse_args(argv)
    path = Path(args.config or os.environ.get(CONFIG_ENV, "").strip() or "config.yml")
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def track_experiment(experiment: Path, params: dict) -> None:
    """Use an independent transform family for each experiment's runner."""
    from lamindb.base.uids import base62_12

    key = f"benchmarks/experiments/{experiment.name}"
    transform = ln.Transform.filter(key=key, is_latest=True).first()
    # An explicit uid stem prevents the SDK's source-hash lookup from renaming
    # another experiment's transform when both execute this same runner file.
    stem = transform.uid[:12] if transform is not None else base62_12()
    ln.track(stem, key=key, path=Path(__file__).resolve(), params=params)


def main(argv: list[str] | None = None) -> None:
    config_path = _config_path(argv)
    params = read_config(config_path)
    experiment = _experiment(params)
    params = resolve_parameters(experiment, params)
    name = run_name(params, default="benchmark")
    try:
        provenance = benchmark_provenance(_ROOT, experiment, config_path)
    except (ValueError, RuntimeError) as error:
        sys.exit(f"Cannot start benchmark: {error}")
    instance = instance_from_env(BENCHMARK_INSTANCE_ENV, BENCHMARK_INSTANCE)

    child_env = os.environ.copy()
    child_env.pop("LAMIN_INITIATED_BY_RUN_UID", None)
    child_env[CONFIG_ENV] = str(config_path)
    # Import Lamin on the stage's own instance so Django's transaction module
    # does not retain stale settings after an instance switch.
    child_env[BENCHMARK_INSTANCE_ENV] = instance_from_env(LOAD_INSTANCE_ENV, LOAD_INSTANCE)

    exit_code = 1
    ln.connect(instance)
    track_experiment(experiment, params)
    try:
        run = ln.context.run
        if run is None:
            sys.exit("benchmark run is missing after ln.track()")
        run.name = name if "name" in params else run.uid
        run.save(update_fields=["name"])
        attach_git_provenance(run, provenance)
        modal_runtime = {
            key: os.environ[variable]
            for key, variable in (
                ("region", "MODAL_REGION"),
                ("cloud_provider", "MODAL_CLOUD_PROVIDER"),
                ("container_id", "MODAL_TASK_ID"),
            )
            if os.environ.get(variable)
        }
        if modal_runtime:
            run.extra_data = {**(run.extra_data or {}), "modal": modal_runtime}
            run.save(update_fields=["extra_data"])
        print(f"Git revision: {run.reference}")
        if provenance["dirty"]:
            print("[warn] benchmark checkout is dirty; the Git link shows the committed "
                  "baseline. Changed files are recorded in run.extra_data['git']['changes'].")
        if not provenance["published"]:
            print("[warn] benchmark commit is not known to be published on origin; "
                  "its Git link may be unavailable until pushed.")
        if not provenance["editable_dependencies_clean"]:
            print("[warn] editable Lamin packages have local changes; their Git references "
                  "do not include those changes (see run.extra_data['git']).")
        record = sync_experiment(experiment, params)
        print(f"experiment: https://lamin.ai/{instance}/record/{record.uid}")
        with tempfile.TemporaryDirectory(prefix="benchmark-results-") as raw_results:
            child_env[RESULTS_DIR_ENV] = raw_results
            exit_code = 0
            for script in stage_scripts(experiment):
                print(f"run {script.name}")
                completed = subprocess.run(
                    [sys.executable, str(script)],
                    check=False,
                    env=child_env,
                )
                exit_code = completed.returncode
                if exit_code != 0:
                    print(f"[warn] {script.name} exited {exit_code}; stopping")
                    break
            artifacts = upload_measurements(
                Path(raw_results),
                instance,
                experiment=experiment,
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
