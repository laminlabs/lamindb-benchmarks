"""Time uid lookups of intervention records on the load instance.

Samples records whose names start with the experiment name. Parameters come
from the benchmark config. Writes a measurement parquet per lookup when the
runner sets BENCHMARK_RESULTS_DIR.

After the timed lookups, records EXPLAIN ANALYZE, table sizes, and server
health. Those facts cannot be reconstructed later: the load database is wiped
between configs. The timed return value is unchanged.

Usage:
  python experiments/experiment_002/query.py
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_HERE = Path(__file__).resolve().parent
for _path in (_ROOT, _HERE):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import lamindb as ln
from observe import BUFFER_FIELDS, explain_lookup, profile_lookup
from parameters import DEFAULT_BATCH, DEFAULT_REPETITIONS
from workload import workload_parameters

from benchmarks.config import (
    LOAD_INSTANCE,
    LOAD_INSTANCE_ENV,
    instance_from_env,
    read_config,
    require_int,
)
from benchmarks.measurements import load_measurements, results_dir, write_measurement
from benchmarks.timing import rows_from_timed, time_calls

NAME_PREFIX = Path(__file__).resolve().parent.name
LOOKUPS = (
    ("uid_lookup", False),
    ("uid_with_features", False),
    ("uid_batch_with_features", True),
)


def main() -> None:
    params = read_config()
    workload = workload_parameters(params)
    rng = random.Random(workload["random_seed"])
    n_rows = require_int(params, "n_rows", minimum=1)
    repetitions = require_int(
        params, "n_repetitions", minimum=1, default=DEFAULT_REPETITIONS
    )
    n_batch = require_int(params, "n_batch", minimum=1, default=DEFAULT_BATCH)
    prefix = f"{NAME_PREFIX}_"

    ln.connect(instance_from_env(LOAD_INSTANCE_ENV, LOAD_INSTANCE))
    # Load-instance finish fails while its Storage row is missing.
    # ln.track(params={"n_rows": n_rows, "n_repetitions": repetitions, "n_batch": n_batch})
    uids = list(
        ln.Record.filter(name__startswith=prefix).order_by("name", "uid").values_list("uid", flat=True)
    )
    if not uids:
        sys.exit(f"no intervention records found for {prefix!r}")
    single = rng.sample(uids, 1)
    batch = rng.sample(uids, min(n_batch, len(uids)))
    sampled_by_query = {}
    for query_name, is_batch in LOOKUPS:
        sampled = batch if is_batch else single
        sampled_by_query[query_name] = sampled
        sql_samples = []

        def measured_lookup(query_name=query_name, sampled=sampled, sql_samples=sql_samples):
            value, stats = profile_lookup(lambda: _lookup(query_name, sampled))
            sql_samples.append(stats)
            return value

        timed = time_calls(measured_lookup, repetitions)
        rows = [
            {**row, **stats}
            for row, stats in zip(rows_from_timed(timed), sql_samples, strict=True)
        ]
        print(
            f"query {query_name} n_uids={len(sampled)} "
            f"rows={timed['last_value']} median={timed['latency_median_ms']} ms "
            f"({repetitions} reps)"
        )
        written = write_measurement(
            query_name,
            rows,
            columns=("repetition", "latency_ms", "value", "sql_execute_count", "sql_execute_ms"),
            key=f"benchmarks/measurements/{NAME_PREFIX}/{query_name}.parquet",
            description=f"Timed {query_name} of intervention records named {prefix}*",
            features={
                "measurement_kind": "lookup",
                "query_name": query_name,
                "name_prefix": prefix,
                "n_rows": n_rows,
                "n_uids": len(sampled),
                "sampled_uids": json.dumps(sampled),
                **workload,
            },
        )
        print(
            "measurement not saved; results directory is unset"
            if written is None
            else f"measurement written: {written.name}"
        )
    _record_explain(sampled_by_query, n_rows, prefix, workload)
    _record_database(n_rows, prefix)
    # ln.finish()


def _record_explain(
    sampled_by_query: dict[str, list[str]], n_rows: int, prefix: str,
    workload: dict[str, int],
) -> None:
    for query_name, _is_batch in LOOKUPS:
        sampled = sampled_by_query[query_name]
        try:
            facts = explain_lookup(
                query_name,
                sampled,
                lambda query_name=query_name, sampled=sampled: _lookup(query_name, sampled),
            )
        except Exception as error:
            print(f"[warn] explain {query_name} failed: {error}")
            continue
        features = {
            "measurement_kind": "explain",
            "query_name": f"explain_{query_name}",
            "source_query": query_name,
            "name_prefix": prefix,
            "n_rows": n_rows,
            "n_uids": len(sampled),
            "used_index": facts["used_index"],
            "used_seq_scan": facts["used_seq_scan"],
            "n_statements": facts["n_statements"],
            "plan_source": facts["plan_source"],
            "query_plan": facts["query_plan"],
            **workload,
            "sampled_uids": json.dumps(sampled),
            **{key: facts[key] for key in BUFFER_FIELDS},
        }
        if facts["execution_time_ms"] is not None:
            features["execution_time_ms"] = facts["execution_time_ms"]
        if facts["planning_time_ms"] is not None:
            features["planning_time_ms"] = facts["planning_time_ms"]
        execution = facts["execution_time_ms"] if facts["execution_time_ms"] is not None else 0.0
        scan = _scan_label(facts["used_index"], facts["used_seq_scan"])
        written = write_measurement(
            f"explain_{query_name}",
            [{
                "repetition": 1,
                "latency_ms": execution,
                "value": facts["n_statements"],
                "sql": facts["sql"],
                "explain_analyze": facts["explain_analyze"],
            }],
            columns=("repetition", "latency_ms", "value", "sql", "explain_analyze"),
            key=f"benchmarks/measurements/{NAME_PREFIX}/explain_{query_name}.parquet",
            description=(
                f"EXPLAIN ANALYZE for {query_name}: {scan} scan, "
                f"execution {execution} ms, from {facts['plan_source']}"
            ),
            features=features,
        )
        print(
            f"explain {query_name} scan={scan} "
            f"execution={facts['execution_time_ms']} ms "
            f"statements={facts['n_statements']} source={facts['plan_source']}"
        )
        print(
            "explain measurement not saved; results directory is unset"
            if written is None
            else f"measurement written: {written.name}"
        )


def _record_database(n_rows: int, prefix: str) -> None:
    from observe import database_facts

    try:
        facts = database_facts()
    except Exception as error:
        print(f"[warn] database facts failed: {error}")
        return
    n_records = ln.Record.filter(name__startswith=prefix).count()
    distribution = facts.pop("distribution")
    features = {
        "measurement_kind": "health",
        "query_name": "health",
        "name_prefix": prefix,
        "n_rows": n_rows,
        "n_records": n_records,
        "n_events": facts["n_events"],
        "n_auxiliary_entries": facts["n_auxiliary_entries"],
        "n_total_rows": facts["n_total_rows"],
        **_insert_rates(),
    }
    if n_records:
        features["write_events_per_record"] = round(facts["n_events"] / n_records, 6)
    for key in ("disk_space_bytes", "cache_hit_ratio", "active_connections"):
        if key in facts:
            features[key] = facts[key]
    written = write_measurement(
        "health",
        [{"repetition": 1, "latency_ms": 0.0, "value": n_records}],
        key=f"benchmarks/measurements/{NAME_PREFIX}/health.parquet",
        description=(
            f"Server health after {n_records} intervention records named {prefix}*"
        ),
        features=features,
    )
    print(
        f"health records={n_records} events={facts['n_events']} "
        f"auxiliary={facts['n_auxiliary_entries']} "
        f"disk={facts.get('disk_space_bytes')} "
        f"cache_hit={facts.get('cache_hit_ratio')}"
    )
    print(
        "health measurement not saved; results directory is unset"
        if written is None
        else f"measurement written: {written.name}"
    )
    for row in distribution:
        table = row["table_name"]
        table_features = {
            "measurement_kind": "distribution",
            "query_name": f"table_{table}",
            "name_prefix": prefix,
            "n_rows": n_rows,
            "table_name": table,
            "table_kind": row["table_kind"],
            "table_rows": row["table_rows"],
            "data_size_bytes": row["data_size_bytes"],
            "index_size_bytes": row["index_size_bytes"],
            "total_size_bytes": row["total_size_bytes"],
            "bloat_ratio": round(row["bloat_ratio"], 6),
        }
        written = write_measurement(
            f"table_{table}",
            [{"repetition": 1, "latency_ms": 0.0, "value": row["table_rows"]}],
            key=f"benchmarks/measurements/{NAME_PREFIX}/table_{table}.parquet",
            description=f"{row['table_kind']} table {table} after {n_rows} intervention records",
            features=table_features,
        )
        if written is None:
            print("distribution measurement not saved; results directory is unset")
            return
    print(f"distribution written for {len(distribution)} tables")


def _insert_rates() -> dict:
    directory = results_dir()
    if directory is None:
        return {}
    for item in load_measurements(directory):
        if item.get("file") != "insert.parquet":
            continue
        features = item.get("features") or {}
        return {
            key: features[key]
            for key in ("insert_seconds", "insert_records_per_sec")
            if key in features
        }
    return {}


def _scan_label(used_index: bool, used_seq_scan: bool) -> str:
    if used_index and not used_seq_scan:
        return "index"
    if used_seq_scan and not used_index:
        return "seq"
    if used_seq_scan and used_index:
        return "mixed"
    return "unknown"


def _lookup(query_name: str, uids: list[str]) -> int:
    if query_name == "uid_batch_with_features":
        frame = ln.Record.filter(uid__in=uids).to_dataframe(include="features")
        return len(frame)
    query = ln.Record.filter(uid=uids[0])
    if query_name == "uid_lookup":
        return 0 if query.one_or_none() is None else 1
    frame = query.to_dataframe(include="features")
    return len(frame)


if __name__ == "__main__":
    main()
