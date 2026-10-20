"""Resolve all effective parameters before the runner starts tracking."""

from benchmarks.config import require_int
from experiments.experiment_002.workload import workload_parameters

DEFAULT_REPETITIONS = 3
DEFAULT_BATCH = 20


def resolve_parameters(params: dict) -> dict:
    return {
        **params,
        **workload_parameters(params),
        "n_repetitions": require_int(
            params, "n_repetitions", minimum=1, default=DEFAULT_REPETITIONS,
        ),
        "n_batch": require_int(params, "n_batch", minimum=1, default=DEFAULT_BATCH),
    }
