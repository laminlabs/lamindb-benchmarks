"""Time a page of Samples ordered by a date stored in RecordJson.

Does not time to_dataframe(). That loads the whole type and can sort in
pandas. The timed statement is the SQL a page would run. No extra index or
table is created.

Usage:
  python experiments/experiment_003/query.py
"""
from __future__ import annotations

import json
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
from benchmarks.measurements import write_measurement
from benchmarks.timing import rows_from_timed, time_calls

NAME = Path(__file__).resolve().parent.name
TYPE_NAME = "Samples"
FEATURE_NAME = "collected_on"
QUERY_NAME = "page_date"
LIMIT = 50
DEFAULT_REPETITIONS = 11


def main() -> None:
    params = read_config()
    n_rows = require_int(params, "n_rows", minimum=1)
    repetitions = require_int(
        params, "n_repetitions", minimum=1, default=DEFAULT_REPETITIONS
    )
    ln.connect(instance_from_env(LOAD_INSTANCE_ENV, LOAD_INSTANCE))
    # ln.track()
    samples, feature = sheet()
    sql, sql_params = page_sql(feature.id, samples.id)
    print(f"page type={samples.id} feature={feature.id} rows={n_rows}")
    timed = time_calls(lambda: run_page(sql, sql_params), repetitions)
    print(
        f"{QUERY_NAME} rows={timed['last_value']} "
        f"median={timed['latency_median_ms']} ms ({repetitions} reps)"
    )
    written = write_measurement(
        QUERY_NAME,
        rows_from_timed(timed),
        key=f"benchmarks/measurements/{NAME}/{QUERY_NAME}.parquet",
        description=(
            f"Page of {LIMIT} Samples ordered by collected_on. "
            f"Median {timed['latency_median_ms']} ms over {repetitions} calls."
        ),
        features={
            "measurement_kind": "page",
            "query_name": QUERY_NAME,
            "n_rows": n_rows,
            "rows_returned": timed["last_value"],
        },
    )
    _print_written(written)
    _write_explain(sql, sql_params, n_rows)
    # ln.finish()


def page_sql(feature_id: int, type_id: int) -> tuple[str, list]:
    """Order by the date, not by the jsonb value.

    ISO dates are stored as JSON strings. jsonb ordering is not date order.
    """
    sql = (
        "SELECT r.id FROM lamindb_record r "
        "LEFT JOIN lamindb_recordjson j "
        "ON j.record_id = r.id AND j.feature_id = %s "
        "WHERE r.type_id = %s "
        "ORDER BY (j.value #>> '{}')::date "
        f"LIMIT {LIMIT}"
    )
    return sql, [feature_id, type_id]


def run_page(sql: str, params: list) -> int:
    with _cursor() as cur:
        cur.execute(sql, params)
        return len(cur.fetchall())


def _write_explain(sql: str, params: list, n_rows: int) -> None:
    plan = explain(sql, params)
    spilled, summary = plan_flags(plan)
    execution = plan.get("Execution Time")
    latency = round(float(execution), 3) if isinstance(execution, int | float) else 0.0
    print(f"explain {summary}")
    written = write_measurement(
        f"explain_{QUERY_NAME}",
        [{"repetition": 1, "latency_ms": latency, "value": int(spilled)}],
        key=f"benchmarks/measurements/{NAME}/explain_{QUERY_NAME}.parquet",
        description=f"EXPLAIN ANALYZE for {QUERY_NAME}. {summary}",
        features={
            "measurement_kind": "explain",
            "query_name": f"explain_{QUERY_NAME}",
            "source_query": QUERY_NAME,
            "n_rows": n_rows,
            "spilled_to_disk": spilled,
            "execution_time_ms": latency,
            "query_plan": summary,
        },
    )
    _print_written(written)


def explain(sql: str, params: list) -> dict:
    with _cursor() as cur:
        cur.execute(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}", params)
        payload = cur.fetchone()[0]
    if isinstance(payload, str):
        payload = json.loads(payload)
    if isinstance(payload, list):
        payload = payload[0]
    if not isinstance(payload, dict):
        sys.exit(f"unexpected explain payload: {payload!r}")
    return payload


def plan_flags(plan: dict) -> tuple[bool, str]:
    nodes = _nodes(plan.get("Plan") or {})
    spilled = any(
        node.get("Sort Space Type") == "Disk"
        or node.get("Sort Method") == "external merge"
        for node in nodes
    )
    methods = sorted(
        {str(node.get("Sort Method")) for node in nodes if node.get("Sort Method")}
    )
    spaces = sorted(
        {
            str(node.get("Sort Space Type"))
            for node in nodes
            if node.get("Sort Space Type")
        }
    )
    scans = sorted(
        {str(node.get("Node Type")) for node in nodes if node.get("Node Type")}
    )
    summary = (
        f"sort={','.join(methods) or 'none'} "
        f"space={','.join(spaces) or 'none'} "
        f"nodes={','.join(scans) or 'none'} "
        f"execution_ms={plan.get('Execution Time')}"
    )
    return spilled, summary


def sheet() -> tuple[ln.Record, ln.Feature]:
    samples = ln.Record.filter(name=TYPE_NAME, is_type=True).one_or_none()
    if samples is None:
        sys.exit(f"record type {TYPE_NAME!r} was not found")
    feature = ln.Feature.filter(name=FEATURE_NAME, is_type=False).one_or_none()
    if feature is None:
        sys.exit(f"feature {FEATURE_NAME!r} was not found")
    return samples, feature


def _nodes(plan: dict) -> list[dict]:
    found = [plan]
    for child in plan.get("Plans") or []:
        found.extend(_nodes(child))
    return found


def _cursor():
    from django.db import connections

    return connections[ln.Record.objects.db].cursor()


def _print_written(written: Path | None) -> None:
    print(
        "measurement not saved; results directory is unset"
        if written is None
        else f"measurement written: {written.name}"
    )


if __name__ == "__main__":
    main()
