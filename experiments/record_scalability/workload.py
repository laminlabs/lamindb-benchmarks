"""Effective parameters shared by population and lookup sampling."""

from __future__ import annotations

import os
import sys
from functools import lru_cache

from benchmarks.config import CONFIG_ENV, read_config, require_int
from experiments.record_scalability.query_parameters import query_parameters


def workload_parameters(params: dict) -> dict[str, int]:
    values = {
        "random_seed": require_int(params, "random_seed", minimum=0, default=0),
        "n_genes": require_int(params, "n_genes", minimum=0, default=100),
        "n_molecules": require_int(params, "n_molecules", minimum=0, default=20),
    }
    for name, defaults, catalog in (
        ("genes", (3, 5), "n_genes"),
        ("small_molecules", (1, 3), "n_molecules"),
    ):
        lower = f"{name}_per_record_min"
        upper = f"{name}_per_record_max"
        values[lower] = require_int(params, lower, minimum=0, default=defaults[0])
        values[upper] = require_int(params, upper, minimum=0, default=defaults[1])
        if not values[lower] <= values[upper] <= values[catalog]:
            sys.exit(
                f"require 0 <= {lower} <= {upper} <= {catalog} ({values[catalog]})"
            )
    return values


@lru_cache(maxsize=1)
def analysis_workload() -> dict[str, int]:
    """Analyze default catalogs/link ranges, or the config selected by env."""
    config = os.environ.get(CONFIG_ENV, "").strip()
    return workload_parameters(read_config(config) if config else {})


def matches_analysis_workload(params: dict) -> bool:
    effective = workload_parameters(params)
    return query_parameters(params) == analysis_queries() and all(
        effective[key] == value
        for key, value in analysis_workload().items()
        if key != "random_seed"
    )


@lru_cache(maxsize=1)
def analysis_queries() -> dict[str, str]:
    config = os.environ.get(CONFIG_ENV, "").strip()
    return query_parameters(read_config(config) if config else {})
