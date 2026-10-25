"""Run experiment 3 and atomically publish its measurements.

Usage: python experiments/experiment_003/run.py CONFIG OUTPUT_DIRECTORY
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.config import read_config
from benchmarks.observations import child_arguments, observation


def main(argv=None):
    args = child_arguments(argv)
    params = read_config(args.config)
    from experiments.experiment_003 import population, queries

    with observation(args.output, f"exp_3_n_rows_{params['n_rows']:_}", {
        "n_rows": params["n_rows"],
    }):
        population.main()
        queries.main()


if __name__ == "__main__":
    main()
