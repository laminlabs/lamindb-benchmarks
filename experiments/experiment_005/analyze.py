"""Run the Notion median/p05/p95 latency analysis."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from experiments.experiment_005.analysis import main

if __name__ == "__main__":
    main("latency_median_p95")
