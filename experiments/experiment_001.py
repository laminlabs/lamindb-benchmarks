"""Example load-instance experiment launched by benchmarks/run_benchmark.py.

Parameters come from ./config.yml in the working directory. The load
instance is LAMIN_LOAD_INSTANCE or laminlabs/lamindb-load-test.

Usage:
  python experiments/experiment_001.py
  python experiments/experiment_001.py setup
  python experiments/experiment_001.py query

One invocation is one tracked run. The optional stage argument limits it to
setup or query. The default runs setup, then query.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import lamindb as ln

from benchmarks.config import (
    LOAD_INSTANCE,
    LOAD_INSTANCE_ENV,
    instance_from_env,
    read_config,
    require_int,
)
from benchmarks.measurements import write_measurement
from benchmarks.setup import create_records
from benchmarks.timing import rows_from_timed, time_calls

NAME_PREFIX = "experiment_001"
DEFAULT_REPETITIONS = 3


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Setup and query example records on the load instance.")
    parser.add_argument(
        "stage",
        nargs="?",
        choices=("setup", "query"),
        help="Run only this stage. Default: setup then query.",
    )
    return parser.parse_args(argv)


def main_setup(n_rows: int) -> None:
    create_records(n_rows, name_prefix=NAME_PREFIX)
    print(f"setup created {n_rows} records")


def main_query(n_rows: int, repetitions: int) -> None:
    prefix = f"{NAME_PREFIX}_"

    def count_records():
        return ln.Record.filter(name__startswith=prefix).count()

    timed = time_calls(count_records, repetitions)
    print(
        f"query name__startswith={prefix} count={timed['last_value']} "
        f"median={timed['latency_median_ms']} ms ({repetitions} reps)"
    )
    written = write_measurement(
        "record_count",
        rows_from_timed(timed),
        key=f"benchmarks/measurements/{NAME_PREFIX}/record_count.parquet",
        description=f"Timed count of Record names starting with {prefix}",
        features={
            "query_name": "record_count",
            "name_prefix": prefix,
            "n_rows": n_rows,
        },
    )
    print(
        "measurement not saved; results directory is unset"
        if written is None
        else f"measurement written: {written.name}"
    )


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    params = read_config()
    run_setup = args.stage in (None, "setup")
    run_query = args.stage in (None, "query")
    n_rows = require_int(params, "n_rows", minimum=0)
    repetitions = (
        require_int(params, "n_repetitions", minimum=1, default=DEFAULT_REPETITIONS)
        if run_query
        else None
    )

    tracked = {"stage": args.stage or "setup,query"}
    if run_setup:
        tracked["n_rows"] = n_rows
    if repetitions is not None:
        tracked["n_repetitions"] = repetitions

    ln.connect(instance_from_env(LOAD_INSTANCE_ENV, LOAD_INSTANCE))
    ln.track(params=tracked)
    try:
        if run_setup:
            main_setup(n_rows)
        if repetitions is not None:
            main_query(n_rows, repetitions)
    finally:
        ln.finish()


if __name__ == "__main__":
    main()
