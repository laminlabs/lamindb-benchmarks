"""Resolve the Notion workload before the generic runner tracks it."""

from __future__ import annotations

import os
from uuid import UUID

from benchmarks.config import require_int
from experiments.experiment_002.workload import workload_parameters

API_VERSION = "2026-03-11"


def resolve_parameters(params: dict) -> dict:
    if any("token" in key.lower() or "api_key" in key.lower() for key in params):
        raise ValueError(
            "Keep credentials in NOTION_API_KEY, never in benchmark config"
        )
    resolved = {
        **params,
        **workload_parameters(params),
        "backend": "notion",
        "n_rows": require_int(params, "n_rows", minimum=1),
        "existing_rows": require_int(params, "existing_rows", minimum=0, default=0),
        "n_repetitions": require_int(params, "n_repetitions", minimum=1, default=11),
        "n_batch": require_int(params, "n_batch", minimum=1, default=20),
        "insert_batch_size": require_int(
            params, "insert_batch_size", minimum=1, default=1000
        ),
        "notion_requests_per_second": require_int(
            params,
            "notion_requests_per_second",
            minimum=1,
            default=3,
        ),
        "notion_max_retries": require_int(
            params, "notion_max_retries", minimum=0, default=5
        ),
        "notion_api_version": params.get("notion_api_version", API_VERSION),
    }
    for key in ("notion_parent_page_id", "notion_dataset_page_id"):
        value = params.get(key) or os.environ.get(key.upper(), "")
        resolved[key] = str(UUID(value)) if value else ""
    if not resolved["notion_api_version"] == API_VERSION:
        raise ValueError(f"This experiment supports Notion-Version {API_VERSION}")
    if resolved["existing_rows"] > resolved["n_rows"]:
        raise ValueError("existing_rows must not exceed n_rows")
    if resolved["existing_rows"] and not resolved["notion_dataset_page_id"]:
        raise ValueError("Resuming requires notion_dataset_page_id")
    if not (resolved["notion_parent_page_id"] or resolved["notion_dataset_page_id"]):
        raise ValueError(
            "Set NOTION_PARENT_PAGE_ID for a fresh dataset, or NOTION_DATASET_PAGE_ID to resume"
        )
    if resolved["n_batch"] > 100 or resolved["insert_batch_size"] > 1000:
        raise ValueError("n_batch must be <=100; insert_batch_size must be <=1000")
    if (
        max(
            resolved["genes_per_record_max"], resolved["small_molecules_per_record_max"]
        )
        > 25
    ):
        raise ValueError(
            "This experiment requires <=25 references per relation property"
        )
    return resolved
