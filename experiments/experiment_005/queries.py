"""Time equivalent Notion lookups and record client-visible population facts."""

from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from benchmarks.config import read_config
from benchmarks.measurements import load_measurements, results_dir
from experiments.experiment_005.client import NotionClient
from experiments.experiment_005.parameters import resolve_parameters
from experiments.experiment_005.population import measurement
from experiments.experiment_005.workload import inventory, plain, uid, uid_filter

LOOKUPS = ("uid_lookup", "uid_with_features", "uid_batch_with_features")
RELATIONS = {
    "supplier": "suppliers",
    "genes": "genes",
    "small_molecules": "small_molecules",
}


def lookup(api, source: str, sampled: list[str], catalogs: dict, *, full: bool) -> int:
    pages = list(
        api.query(
            source,
            filter=uid_filter(sampled),
            properties=None if full else ["benchmark_uid"],
        )
    )
    actual = [plain(page["properties"]["benchmark_uid"]) for page in pages]
    if len(actual) != len(sampled) or set(actual) != set(sampled):
        raise ValueError(
            "Lookup returned missing, duplicate, or unexpected interventions"
        )
    if full:
        names = {
            table: {page_id: name for name, page_id in entries.items()}
            for table, entries in catalogs.items()
        }
        rows = []
        for page in pages:
            props = page["properties"]
            row = {
                "uid": plain(props["benchmark_uid"]),
                "dose": plain(props["dose"]),
                "protocol": props["protocol"]["select"]["name"],
                "treatment_date": props["treatment_date"]["date"]["start"],
                "treatment_time_min": props["treatment_time_min"]["number"],
            }
            for prop, table in RELATIONS.items():
                if props[prop].get("has_more"):
                    raise ValueError(
                        "Truncated relation values cannot be benchmarked as complete features"
                    )
                row[prop] = [names[table][ref["id"]] for ref in props[prop]["relation"]]
            rows.append(row)
        # Include decoding and DataFrame construction, as in record scalability.
        return len(pd.DataFrame(rows))
    return len(pages)


def record_queries(api, params: dict, state: dict):
    rng = random.Random(params["random_seed"])
    single = [uid(index) for index in rng.sample(range(params["n_rows"]), 1)]
    batch = [
        uid(index)
        for index in rng.sample(
            range(params["n_rows"]), min(params["n_batch"], params["n_rows"])
        )
    ]
    source = state["sources"]["interventions"]["data_source_id"]
    # Notion's query index can lag page creation. Probe outside the timer.
    for attempt in range(10):
        found = list(
            api.query(
                source,
                filter=uid_filter([uid(params["n_rows"] - 1)]),
                properties=["benchmark_uid"],
            )
        )
        if found:
            break
        time.sleep(3)
    else:
        raise ValueError("The final inserted intervention is not yet queryable")
    for name in LOOKUPS:
        sampled = batch if name == "uid_batch_with_features" else single
        rows = []
        api.operation, api.batch_index = name, 0
        try:
            for repetition in range(1, params["n_repetitions"] + 1):
                api.repetition = repetition
                before, started = api.snapshot(), time.perf_counter()
                value = lookup(
                    api, source, sampled, state["catalogs"], full=name != "uid_lookup"
                )
                rows.append(
                    {
                        "repetition": repetition,
                        "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                        "value": value,
                        **api.delta(before),
                    }
                )
        finally:
            if rows:
                measurement(
                    name,
                    rows,
                    params,
                    features={
                        "measurement_kind": "lookup",
                        "n_uids": len(sampled),
                        "sampled_uids": json.dumps(sampled),
                        "data_source_id": source,
                    },
                    description="UID property filter query, including HTTP, pacing/retry waits, decoding and DataFrame construction (with features).",
                )
        print(f"{name}: {len(rows)} repetitions completed", flush=True)
        measurement(
            f"explain_{name}",
            [
                {
                    "repetition": 1,
                    "latency_ms": float("nan"),
                    "value": float("nan"),
                    "supported": False,
                    "reason": "Notion exposes no SQL/EXPLAIN, server execution time, scan plan or buffer counters.",
                }
            ],
            params,
            features={
                "measurement_kind": "explain",
                "supported": False,
                "source_query": name,
            },
            description="Explicit unavailable marker, not a simulated EXPLAIN measurement. See query_requests.parquet for HTTP telemetry.",
        )


def record_health(api, params: dict, state: dict):
    api.operation, api.repetition, api.batch_index = "inventory", 0, 0
    source = state["sources"]["interventions"]["data_source_id"]
    links = dict.fromkeys(RELATIONS, 0)
    count = 0
    for page in inventory(api, source, params["n_rows"], full=True):
        count += 1
        for prop in links:
            if page["properties"][prop].get("has_more"):
                raise ValueError("Cannot count truncated relations")
            references = page["properties"][prop]["relation"]
            lower, upper = (
                (1, 1)
                if prop == "supplier"
                else (
                    params[f"{prop}_per_record_min"],
                    params[f"{prop}_per_record_max"],
                )
            )
            if not lower <= len(references) <= upper:
                raise ValueError(f"Unexpected or inaccessible {prop} relation values")
            valid_ids = set(state["catalogs"][RELATIONS[prop]].values())
            if any(ref["id"] not in valid_ids for ref in references):
                raise ValueError(f"Unexpected {prop} catalog reference")
            links[prop] += len(references)
    for table, names in state["catalogs"].items():
        pages = list(api.query(state["sources"][table]["data_source_id"]))
        if {plain(p["properties"]["name"]): p["id"] for p in pages} != names:
            raise ValueError(f"{table} catalog changed during the benchmark")
    counts = {table: len(names) for table, names in state["catalogs"].items()}
    counts["interventions"] = count
    facts = {
        "measurement_kind": "health",
        "n_records": count,
        "n_catalog_records": sum(counts.values()) - count,
        "n_relation_links": sum(links.values()),
        "server_metrics_available": False,
        "dataset_page_id": state["dataset_page_id"],
        "http_request_count": len(api.rows),
    }
    inserts = [
        entry
        for entry in load_measurements(results_dir())
        if entry["file"] == "insert.parquet"
    ]
    if inserts:
        for key in (
            "insert_seconds",
            "insert_records_per_sec",
            "insert_retry_count",
            "insert_batch_count",
        ):
            if key in inserts[0]["features"]:
                facts[key] = inserts[0]["features"][key]
        facts["n_inserted_records"] = inserts[0]["features"]["n_records"]
    # Missing server values remain null in the parquet, and are absent from metadata.
    measurement(
        "health",
        [
            {
                "repetition": 1,
                "latency_ms": 0.0,
                "value": count,
                "disk_space_bytes": None,
                "cache_hit_ratio": None,
                "active_connections": None,
                "n_events": None,
            }
        ],
        params,
        features=facts,
        description="Exact API-visible page and relation counts. Notion server disk/cache/write-log statistics are unavailable.",
    )
    for table, rows in counts.items():
        measurement(
            f"table_{table}",
            [
                {
                    "repetition": 1,
                    "latency_ms": 0.0,
                    "value": rows,
                    "data_size_bytes": None,
                    "index_size_bytes": None,
                    "total_size_bytes": None,
                }
            ],
            params,
            features={
                "measurement_kind": "distribution",
                "table_name": table,
                "table_rows": rows,
                "data_source_id": state["sources"][table]["data_source_id"],
            },
            description="Exact logical data-source page count; not a physical SQL table or storage size.",
        )
    for prop, rows in links.items():
        measurement(
            f"table_links_{prop}",
            [{"repetition": 1, "latency_ms": 0.0, "value": rows}],
            params,
            features={
                "measurement_kind": "distribution",
                "table_name": f"links_{prop}",
                "table_rows": rows,
            },
            description="Logical relation references, counted via page properties.",
        )
    print(
        f"Verified {count} interventions; {sum(links.values())} relation references",
        flush=True,
    )


def main():
    params = resolve_parameters(read_config())
    directory = results_dir()
    if directory is None:
        raise ValueError("Run this experiment using benchmarks/run_benchmark.py")
    state = json.loads((directory / "notion_state.json").read_text())
    if state["completed_rows"] != params["n_rows"]:
        raise ValueError("Population is incomplete; refusing to time queries")
    api = NotionClient(params)
    try:
        record_queries(api, params, state)
        record_health(api, params, state)
    finally:
        api.close("query")
