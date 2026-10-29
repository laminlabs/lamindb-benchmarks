"""Track one experiment execution and upload completed observations while it runs.

The child receives two positional arguments: CONFIG OUTPUT_DIRECTORY.
Only this parent writes tracking records/artifacts to the benchmark instance.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from benchmarks.config import (
    BENCHMARK_INSTANCE,
    BENCHMARK_INSTANCE_ENV,
    CONFIG_ENV,
    LOAD_INSTANCE,
    LOAD_INSTANCE_ENV,
    execution_environment,
    instance_from_env,
    read_config,
    resolve_parameters,
    run_name,
)
from benchmarks.observations import read_observation, validate_features, validate_name
from benchmarks.provenance import attach_git_provenance, benchmark_provenance


def _experiment(params: dict) -> Path:
    raw = params.get("experiment")
    if not isinstance(raw, str) or not raw.strip():
        sys.exit("parameters must include experiment: path to an experiment folder")
    path = Path(raw).resolve()
    if not path.is_relative_to(_ROOT) or not path.is_dir():
        raise ValueError("Experiment must be a folder inside this repository")
    for name in ("README.md", "run.py"):
        if not (path / name).is_file():
            raise ValueError(f"Experiment is missing {name}: {path}")
    return path


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


def track_experiment(ln, experiment: Path, params: dict) -> None:
    """Use an independent transform family for each experiment's runner."""
    from lamindb.base.uids import base62_12

    key = f"benchmarks/experiments/{experiment.name}"
    transform = ln.Transform.filter(key=key, is_latest=True).first()
    # An explicit uid stem prevents the SDK's source-hash lookup from renaming
    # another experiment's transform when both execute this same runner file.
    stem = transform.uid[:12] if transform is not None else base62_12()
    ln.track(stem, key=key, path=experiment / "run.py", params=params, new_run=True)


class ObservationUploader:
    """Single parent-process writer, with idempotent keys for upload retries."""

    def __init__(self, ln, experiment, run_record, experiment_path, instance):
        self.ln, self.experiment, self.run_record = ln, experiment, run_record
        self.experiment_path, self.instance = experiment_path, instance
        self.completed = set()
        self.failures = {}

    def upload(self, folder):
        from benchmarks.experiment import feature_dtype, record_observation

        ln = self.ln
        values, notes, files = read_observation(folder)
        for value in values.values():
            if value is not None:
                feature_dtype(value)
        artifacts = []
        for path in files:
            relative = path.relative_to(folder).as_posix()
            key = (
                f"benchmarks/measurements/{self.experiment_path.name}/"
                f"{ln.context.run.uid}/observations/{folder.name}/{relative}"
            )
            artifact = ln.Artifact.filter(run=ln.context.run, key=key).one_or_none()
            if artifact is None:
                constructor = (
                    ln.Artifact.from_dataframe
                    if path.suffix == ".parquet"
                    else ln.Artifact
                )
                artifact = constructor(
                    path,
                    key=key,
                    run=ln.context.run,
                    skip_hash_lookup=True,
                    description=f"{folder.name}: {relative}",
                ).save()
            artifacts.append(artifact)
        record = record_observation(
            self.experiment,
            self.run_record,
            folder.name,
            values,
            artifacts,
            notes,
            self.instance,
        )
        print(
            f"observation: https://lamin.ai/{self.instance}/record/{record.uid}",
            flush=True,
        )

    def upload_run_information(self, path):
        from benchmarks.experiment import config_columns, record_run

        if path.is_symlink() or not path.is_file():
            raise ValueError("run_info.yml must be a real file")
        values = validate_features(yaml.safe_load(path.read_text()))
        if set(config_columns(values)) != set(values):
            raise ValueError(
                "Run information must contain only non-null feature values"
            )
        run = self.ln.context.run
        if set(values) & set(run.params or {}):
            raise ValueError("Run information must not overwrite config parameters")
        existing = (run.extra_data or {}).get("run_info")
        if existing is not None and existing != values:
            raise ValueError("Run information cannot change within an execution")
        run.extra_data = {**(run.extra_data or {}), "run_info": values}
        run.save(update_fields=["extra_data"])
        self.run_record = record_run(
            self.experiment,
            run.params or {},
            [],
            instance=self.instance,
        )
        print(f"run information saved: {values}", flush=True)

    def attempt(self, name, upload):
        failed, retry_at = self.failures.get(name, (0, 0))
        if name in self.completed or time.monotonic() < retry_at:
            return
        try:
            upload()
        except (ValueError, TypeError):
            raise
        except Exception as error:
            failed += 1
            if failed >= 3:
                raise RuntimeError(
                    f"Result {name} upload failed three times"
                ) from error
            self.failures[name] = (failed, time.monotonic() + 2**failed)
            print(
                f"[warn] result {name}: upload attempt {failed} failed; retrying",
                flush=True,
            )
            return
        self.completed.add(name)
        self.failures.pop(name, None)

    def drain(self, output):
        information = output / "run_info.yml"
        if information.exists() or information.is_symlink():
            self.attempt(".run_info", lambda: self.upload_run_information(information))
        ready = output / "observations"
        if not ready.exists():
            return
        if ready.is_symlink() or not ready.is_dir():
            raise ValueError("observations must be a real directory")
        for folder in sorted(ready.iterdir()):
            if folder.name.startswith(".") or folder.name in self.completed:
                continue
            validate_name(folder.name)
            self.attempt(folder.name, lambda folder=folder: self.upload(folder))


def run_child(command, env, output, uploader):
    """Inherit output streams (no pipe deadlocks) and poll completed bundles."""
    process = subprocess.Popen(command, env=env, cwd=_ROOT)
    try:
        while True:
            uploader.drain(output)
            code = process.poll()
            if code is not None:
                # One final drain catches an observation published at exit.
                uploader.drain(output)
                while uploader.failures:
                    time.sleep(1)
                    uploader.drain(output)
                return code
            time.sleep(1)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def main(argv: list[str] | None = None) -> None:
    import lamindb_setup

    config_path = _config_path(argv)
    params = read_config(config_path)
    # Validate everything before tracking or launching database writes.
    experiment = _experiment(params)
    params = resolve_parameters(experiment, params)
    name = run_name(params, default="benchmark")
    environment = execution_environment()
    provenance = benchmark_provenance(_ROOT, experiment, config_path)
    instance = instance_from_env(BENCHMARK_INSTANCE_ENV, BENCHMARK_INSTANCE)
    load_instance = instance_from_env(LOAD_INSTANCE_ENV, LOAD_INSTANCE)
    if instance == load_instance:
        raise ValueError("Tracking and load instances must be different")
    lamindb_setup.connect(instance)
    import lamindb as ln

    from benchmarks.experiment import config_columns, record_run, sync_experiment

    config_columns(params)
    track_experiment(ln, experiment, params)
    output = Path(tempfile.mkdtemp(prefix="benchmark-results-"))
    exit_code, run_record, uploader = 1, None, None
    try:
        run = ln.context.run
        run.name = name if "name" in params else run.uid
        run.save(update_fields=["name"])
        attach_git_provenance(run, provenance)
        runtime = {
            key: os.environ[var]
            for key, var in (
                ("region", "MODAL_REGION"),
                ("cloud_provider", "MODAL_CLOUD_PROVIDER"),
                ("container_id", "MODAL_TASK_ID"),
            )
            if os.environ.get(var)
        }
        run.extra_data = {
            **(run.extra_data or {}),
            "environment": environment,
            **({"modal": runtime} if runtime else {}),
        }
        run.save(update_fields=["extra_data"])
        record = sync_experiment(experiment, params)
        # Create the run record before the child starts, initially unsuccessful.
        run_record = record_run(record, params, [], instance=instance, exit_code=None)
        uploader = ObservationUploader(ln, record, run_record, experiment, instance)
        print(f"run: https://lamin.ai/{instance}/record/{run_record.uid}", flush=True)
        print(f"output directory: {output}", flush=True)
        if provenance["dirty"] or not provenance["published"]:
            print(
                "[warn] Git links show the committed baseline; local changes/publication status are recorded in run.extra_data['git'].",
                flush=True,
            )
        env = os.environ.copy()
        env.pop("LAMIN_INITIATED_BY_RUN_UID", None)
        env[CONFIG_ENV] = str(config_path)
        env["BENCHMARK_TRACKING_INSTANCE"] = instance
        env[BENCHMARK_INSTANCE_ENV] = load_instance
        env[LOAD_INSTANCE_ENV] = load_instance
        env["PYTHONUNBUFFERED"] = "1"
        exit_code = run_child(
            [sys.executable, str(experiment / "run.py"), str(config_path), str(output)],
            env,
            output,
            uploader,
        )
    finally:
        try:
            if run_record is not None:
                record_run(record, params, [], instance=instance, exit_code=exit_code)
            if exit_code == 0:
                shutil.rmtree(output)
            else:
                print(
                    f"[warn] run incomplete; local results retained at {output}",
                    flush=True,
                )
        finally:
            tracked = ln.context.run
            try:
                ln.finish()
            finally:
                if tracked is not None:
                    tracked.extra_data = {
                        **(tracked.extra_data or {}),
                        "exit_code": exit_code,
                    }
                    if exit_code != 0:
                        # finish() otherwise marks a normally-returned parent as
                        # completed even when its experiment subprocess failed.
                        tracked._status_code = 1
                    tracked.save(update_fields=["extra_data", "_status_code"])
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
