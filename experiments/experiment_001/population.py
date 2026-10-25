"""Example load-instance experiment launched by benchmarks/run_benchmark.py.

Creates n_rows records on the load instance. Parameters come from the
benchmark config. The load instance is LAMIN_LOAD_INSTANCE or
laminlabs/lamindb-load-test.

Usage:
  python experiments/experiment_001/run.py CONFIG OUTPUT_DIRECTORY
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
from benchmarks.setup import create_records

NAME_PREFIX = Path(__file__).resolve().parent.name


def main() -> None:
    n_rows = require_int(read_config(), "n_rows", minimum=0)
    ln.connect(instance_from_env(LOAD_INSTANCE_ENV, LOAD_INSTANCE))
    # Load-instance finish fails while its Storage row is missing.
    # ln.track(params={"n_rows": n_rows})
    create_records(n_rows, name_prefix=NAME_PREFIX)
    print(f"setup created {n_rows} records")
    # ln.finish()
