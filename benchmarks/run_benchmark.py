"""Connect to the benchmark instance, track parameters, run experiment_script.

Parameters come from ./config.yml in the working directory. Every key is
passed to ln.track(params=...). The file itself is not uploaded. The benchmark
instance is LAMIN_CURRENT_INSTANCE or laminlabs/lamindb-benchmarks.
experiment_script is a path to a Python file, launched in a separate process,
and is not a run-record column. Measurement parquet files it writes are
uploaded as outputs of this run and linked to a run record on the experiment
frame. Tracking ends after that process exits.
"""
from __future__ import annotations

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


def upload_measurements(directory: Path, instance: str) -> list[ln.Artifact]:
    artifacts = []
    for item in load_measurements(directory):
        features = {
            _ensure_feature(name, _feature_dtype(value)): value
            for name, value in item["features"].items()
        }
        artifact = ln.Artifact.from_dataframe(
            item["path"],
            key=item["key"],
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


def main() -> None:
    params = read_config()
    experiment_script = _experiment_script(params)
    instance = instance_from_env(BENCHMARK_INSTANCE_ENV, BENCHMARK_INSTANCE)

    child_env = os.environ.copy()
    child_env.pop("LAMIN_INITIATED_BY_RUN_UID", None)

    exit_code = 1
    ln.connect(instance)
    ln.track(params=params)
    try:
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
            artifacts = upload_measurements(Path(raw_results), instance)
            run_record = record_run(
                record, params, artifacts, instance=instance, exit_code=exit_code
            )
            print(f"run: https://lamin.ai/{instance}/record/{run_record.uid}")
    finally:
        ln.finish()
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
