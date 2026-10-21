"""Populate a fresh or explicitly resumed Notion dataset. Never resets anything."""

from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.config import read_config
from benchmarks.measurements import results_dir, write_measurement
from experiments.experiment_002.workload import workload_parameters
from experiments.experiment_005.client import NotionClient
from experiments.experiment_005.parameters import resolve_parameters
from experiments.experiment_005.workload import (
    DATASET_TITLE,
    NAME,
    TABLES,
    catalog_names,
    inventory,
    page_properties,
    plain,
    schema,
    text,
    treatment,
)


def measurement(
    name: str,
    rows: list[dict],
    params: dict,
    *,
    features=None,
    description="",
    columns=None,
):
    return write_measurement(
        name,
        rows,
        columns=columns or tuple(rows[0]),
        key=f"benchmarks/measurements/{NAME}/{name}.parquet",
        description=description
        or f"Notion {name}; client API measurements, not database server metrics.",
        features={
            "measurement_kind": name,
            "query_name": name,
            "backend": "notion",
            "n_rows": params["n_rows"],
            **workload_parameters(params),
            **(features or {}),
        },
    )


def write_state(state: dict):
    directory = results_dir()
    if directory is None:
        raise ValueError(
            "Use benchmarks/run_benchmark.py to provide the shared results directory"
        )
    (directory / "notion_state.json").write_text(json.dumps(state, indent=2) + "\n")


def ensure_dataset(api, params: dict) -> tuple[str, dict]:
    dataset = params["notion_dataset_page_id"]
    if dataset:
        page = api.request("GET", f"pages/{dataset}", read_only=True)
        title = next(
            (plain(v) for v in page["properties"].values() if v["type"] == "title"), ""
        )
        if title != DATASET_TITLE:
            raise ValueError("Resume target is not an experiment_005 dataset page")
    else:
        page = api.request(
            "POST",
            "pages",
            body={
                "parent": {
                    "type": "page_id",
                    "page_id": params["notion_parent_page_id"],
                },
                "properties": {"title": {"title": text(DATASET_TITLE)}},
            },
        )
        dataset = page["id"]
    # Print the durable target immediately, even if later setup fails.
    print(
        f"Notion dataset: https://www.notion.so/{dataset.replace('-', '')}", flush=True
    )
    write_state({"dataset_page_id": dataset, "sources": {}})
    databases = {}
    for block in api.children(dataset):
        if block["type"] == "child_database":
            title = block["child_database"]["title"]
            if title in databases or title not in TABLES:
                raise ValueError("Dataset has duplicate or unexpected databases")
            databases[title] = block["id"]
    if params["existing_rows"] and set(databases) != set(TABLES):
        raise ValueError("A resumed dataset must contain all four benchmark databases")
    sources = {}
    for table in TABLES:
        expected = schema(table, sources)
        if table in databases:
            database = api.request(
                "GET", f"databases/{databases[table]}", read_only=True
            )
        else:
            database = api.request(
                "POST",
                "databases",
                body={
                    "parent": {"type": "page_id", "page_id": dataset},
                    "title": text(table),
                    "is_inline": True,
                    "initial_data_source": {"properties": expected},
                },
            )
        if len(database.get("data_sources", [])) != 1:
            raise ValueError(f"Expected one data source in {table}")
        source_id = database["data_sources"][0]["id"]
        sources[table] = {"database_id": database["id"], "data_source_id": source_id}
        actual = api.request("GET", f"data_sources/{source_id}", read_only=True)[
            "properties"
        ]
        for name, spec in expected.items():
            kind = next(iter(spec))
            if actual.get(name, {}).get("type") != kind:
                raise ValueError(f"Notion schema mismatch: {table}.{name}")
            if kind == "relation" and actual[name][kind].get(
                "data_source_id", ""
            ).replace("-", "") != spec[kind]["data_source_id"].replace("-", ""):
                raise ValueError(f"Notion relation target mismatch: {table}.{name}")
        write_state({"dataset_page_id": dataset, "sources": sources})
    return dataset, sources


def ensure_catalogs(api, params: dict, sources: dict) -> dict:
    catalogs = {}
    for table, names in catalog_names(params).items():
        source = sources[table]["data_source_id"]
        existing = {}
        for page in api.query(source):
            name = plain(page["properties"]["name"])
            if name in existing or name not in names:
                raise ValueError(f"Unexpected or duplicate {table} catalog entry")
            existing[name] = page["id"]
        if params["existing_rows"] and set(existing) != set(names):
            raise ValueError(f"Resume workload does not match the {table} catalog")
        for name in names:
            if name not in existing:
                existing[name] = api.request(
                    "POST",
                    "pages",
                    body={
                        "parent": {"type": "data_source_id", "data_source_id": source},
                        "properties": {"name": {"title": text(name)}},
                    },
                )["id"]
        catalogs[table] = existing
    return catalogs


def populate(api, params: dict, source: str, catalogs: dict, state: dict):
    existing, target = params["existing_rows"], params["n_rows"]
    # Verify exact committed rows rather than accepting a row-count estimate.
    for _ in inventory(api, source, existing):
        pass
    rng = random.Random(params["random_seed"])
    for index in range(existing):
        treatment(index, rng, params)
    batches = []
    created = 0
    try:
        while existing + created < target:
            batch_index = len(batches) + 1
            count = min(params["insert_batch_size"], target - existing - created)
            api.operation, api.batch_index = "insert", batch_index
            snapshot, started = api.snapshot(), time.perf_counter()
            before, saved = existing + created, 0
            succeeded = False
            try:
                for index in range(before, before + count):
                    props = page_properties(treatment(index, rng, params), catalogs)
                    api.request(
                        "POST",
                        "pages",
                        body={
                            "parent": {
                                "type": "data_source_id",
                                "data_source_id": source,
                            },
                            "properties": props,
                        },
                    )
                    saved += 1
                succeeded = True
            finally:
                created += saved
                stats = api.delta(snapshot)
                batches.append(
                    {
                        "repetition": batch_index,
                        "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                        "value": saved,
                        "batch_index": batch_index,
                        "batch_size": count,
                        "records_before": before,
                        "records_after": existing + created,
                        "succeeded": succeeded,
                        **stats,
                    }
                )
                state["completed_rows"] = existing + created
                write_state(state)
                measurement(
                    "insert_batches",
                    batches,
                    params,
                    features={"existing_rows": existing},
                    description="Sequential page-create groups, not atomic batches. A failed group may be partly committed.",
                )
                print(
                    f"Batch {batch_index}: {saved}/{count} inserted; {existing + created}/{target} records; {batches[-1]['latency_ms']:.1f} ms",
                    flush=True,
                )
    finally:
        seconds = sum(b["latency_ms"] for b in batches) / 1000
        features = {
            "n_records": created,
            "existing_rows": existing,
            "insert_seconds": seconds,
            "insert_batch_count": len(batches),
            "insert_retry_count": sum(b["retry_count"] for b in batches),
        }
        if seconds:
            features["insert_records_per_sec"] = created / seconds
        measurement(
            "insert",
            [{"repetition": 1, "latency_ms": seconds * 1000, "value": created}],
            params,
            features=features,
            description="Time for this run's new pages only, including pacing/retries, excluding catalogs, inventory and checkpoint I/O.",
        )


def main():
    params = resolve_parameters(read_config())
    if results_dir() is None:
        raise ValueError("Run this experiment using benchmarks/run_benchmark.py")
    api = NotionClient(params)
    try:
        dataset, sources = ensure_dataset(api, params)
        target_rows = [
            {"table_name": table, "dataset_page_id": dataset, **source}
            for table, source in sources.items()
        ]
        measurement(
            "target",
            target_rows,
            params,
            features={"dataset_page_id": dataset},
            description="Durable Notion target IDs for inspection and explicit resume; contains no credentials.",
        )
        catalogs = ensure_catalogs(api, params, sources)
        state = {
            "dataset_page_id": dataset,
            "sources": sources,
            "catalogs": catalogs,
            "completed_rows": params["existing_rows"],
        }
        write_state(state)
        populate(
            api, params, sources["interventions"]["data_source_id"], catalogs, state
        )
    finally:
        api.close("setup")


if __name__ == "__main__":
    main()
