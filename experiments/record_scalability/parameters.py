"""Resolve all effective parameters before the runner starts tracking."""

from itertools import pairwise

from benchmarks.config import require_int
from experiments.record_scalability.query_parameters import query_parameters
from experiments.record_scalability.workload import workload_parameters

DEFAULT_REPETITIONS = 11
DEFAULT_BATCH = 20


def milestones(value) -> list[int]:
    targets = (
        [value] if isinstance(value, int) and not isinstance(value, bool) else value
    )
    if not isinstance(targets, list) or not targets:
        raise ValueError(
            "n_rows must be a positive integer or nonempty list of positive integers"
        )
    if any(type(target) is not int or target < 1 for target in targets):
        raise ValueError("n_rows milestones must be positive integers")
    if any(left >= right for left, right in pairwise(targets)):
        raise ValueError(
            "n_rows milestones must be strictly increasing, with no duplicates"
        )
    return targets.copy()


def resolve_parameters(params: dict) -> dict:
    if "existing_rows" in params:
        require_int(params, "existing_rows", minimum=0)
    workload = workload_parameters(params)
    queries = query_parameters(params)
    query_gene = queries["query_gene"]
    if query_gene not in (f"gene_{index:03d}" for index in range(workload["n_genes"])):
        raise ValueError(
            "query_gene must name a gene in the configured catalog; n_genes must be positive"
        )
    return {
        **params,
        "n_rows": milestones(params.get("n_rows")),
        **workload,
        **queries,
        "n_repetitions": require_int(
            params,
            "n_repetitions",
            minimum=1,
            default=DEFAULT_REPETITIONS,
        ),
        "n_batch": require_int(params, "n_batch", minimum=1, default=DEFAULT_BATCH),
    }
