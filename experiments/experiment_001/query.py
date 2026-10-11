"""Time a count of experiment_001 records on the load instance.

Counts Record names starting with the experiment name. Parameters come from
the benchmark config. Writes a measurement parquet when the runner sets
BENCHMARK_RESULTS_DIR.

Usage:
  python experiments/experiment_001/query.py
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
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
from benchmarks.timing import rows_from_timed, time_calls

NAME_PREFIX = Path(__file__).resolve().parent.name
DEFAULT_REPETITIONS = 3


def main() -> None:
    params = read_config()
    n_rows = require_int(params, "n_rows", minimum=0)
    repetitions = require_int(
        params, "n_repetitions", minimum=1, default=DEFAULT_REPETITIONS
    )
    prefix = f"{NAME_PREFIX}_"

    def count_records():
        return ln.Record.filter(name__startswith=prefix).count()

    ln.connect(instance_from_env(LOAD_INSTANCE_ENV, LOAD_INSTANCE))
    # Load-instance finish fails while its Storage row is missing.
    # ln.track(params={"n_rows": n_rows, "n_repetitions": repetitions})
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
    # ln.finish()


if __name__ == "__main__":
    main()
