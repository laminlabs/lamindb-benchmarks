"""Load-instance facts and EXPLAIN diagnostics captured at each milestone.

These measurements describe the database at observation time, before the
experiment resumes insertion toward its next population target.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable

import lamindb as ln
import sqlparse

LINK_TABLES = {"lamindb_recordulabel", "lamindb_recordrecord"}
DATA_TABLES = {"lamindb_recordjson"}
EVENTS_TABLE = "hubmodule_dbwrite"
RECORD_TABLES = (
    "lamindb_record",
    "lamindb_recordjson",
    "lamindb_recordrecord",
    "lamindb_recordulabel",
)
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PLAN_LIMIT = 1000
BUFFER_FIELDS = {
    "shared_hit_blocks": "Shared Hit Blocks",
    "shared_read_blocks": "Shared Read Blocks",
    "shared_dirtied_blocks": "Shared Dirtied Blocks",
    "shared_written_blocks": "Shared Written Blocks",
    "local_hit_blocks": "Local Hit Blocks",
    "local_read_blocks": "Local Read Blocks",
    "local_dirtied_blocks": "Local Dirtied Blocks",
    "local_written_blocks": "Local Written Blocks",
    "temp_read_blocks": "Temp Read Blocks",
    "temp_written_blocks": "Temp Written Blocks",
}


def profile_lookup(replay: Callable[[], object]) -> tuple[object, dict]:
    """Count driver executions and time execute calls, including network waits.

    Do not retain SQL or parameters during timing. Fetching and DataFrame
    construction remain in end-to-end latency, outside these execute timings.
    """
    from django.db import connections

    count = 0
    elapsed = 0.0

    def measure(execute, sql, params, many, context):
        nonlocal count, elapsed
        count += 1
        started = time.perf_counter()
        try:
            return execute(sql, params, many, context)
        finally:
            elapsed += time.perf_counter() - started

    with connections[ln.Record.objects.db].execute_wrapper(measure):
        value = replay()
    return value, {
        "sql_execute_count": count,
        "sql_execute_ms": round(elapsed * 1000, 3),
    }


def table_kind(name: str) -> str:
    if name in LINK_TABLES or (
        name.startswith("lamindb_record")
        and name not in {"lamindb_record", *DATA_TABLES}
    ):
        return "link"
    if name in DATA_TABLES:
        return "data"
    return "base"


def count_rows(table: str) -> int | None:
    try:
        with _cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {_quote(table)}")
            return int(cur.fetchone()[0])
    except Exception:  # noqa: BLE001 — best-effort diagnostics across database drivers
        return None


def database_facts() -> dict:
    """Exact row counts for record-related tables, plus server health."""
    tables = _distribution()
    health = _server_health()
    n_events = next(
        (row["table_rows"] for row in tables if row["table_name"] == EVENTS_TABLE),
        0,
    )
    auxiliary = [row for row in tables if row["table_kind"] in {"link", "data"}]
    return {
        "distribution": tables,
        "n_events": int(n_events),
        "n_auxiliary_entries": int(sum(row["table_rows"] for row in auxiliary)),
        "n_total_rows": int(sum(row["table_rows"] for row in tables)),
        **health,
    }


def explain_lookup(
    query_name: str, uids: list[str], replay: Callable[[], object]
) -> dict:
    """EXPLAIN ANALYZE the data SELECT statements issued by one operation.

    The replay is the same lookup that was timed, run once more outside the
    timer. If its statements cannot be captured, the record filter alone is
    explained, which is the plan the archived benchmark recorded.
    """
    statements = _record_selects(replay)
    captured = [(sql, _explain_sql(sql)) for sql in statements]
    explained = [(sql, plan) for sql, plan in captured if plan is not None]
    plans = [plan for _sql, plan in explained]
    source = "lookup"
    if not plans:
        fallback = _explain_queryset(query_name, uids)
        plans = [fallback] if fallback is not None else []
        source = "record_filter"
    facts = _aggregate(plans, source)
    # Keep one SQL example and one complete replay's plans in the artifact body.
    # The fallback plan has no captured SQL; do not label it as an actual lookup.
    facts["sql"] = explained[0][0] if explained else ""
    facts["explain_analyze"] = json.dumps(plans, indent=2)
    facts["statements"] = [
        {"sql": sql, "plan": plan, "source": "lookup"} for sql, plan in captured
    ]
    if source == "record_filter":
        facts["statements"].append({"sql": None, "plan": plans[0], "source": source})
    return facts


def _distribution() -> list[dict]:
    sql = """
    SELECT c.relname,
           pg_relation_size(c.oid),
           pg_indexes_size(c.oid),
           pg_total_relation_size(c.oid),
           COALESCE(s.n_dead_tup, 0),
           COALESCE(s.n_live_tup, 0)
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
    WHERE c.relkind = 'r'
      AND n.nspname = 'public'
      AND (c.relname LIKE %s OR c.relname = %s)
    ORDER BY c.relname
    """
    rows = []
    with _cursor() as cur:
        cur.execute(sql, ("lamindb_record%", EVENTS_TABLE))
        fetched = cur.fetchall()
        for name, data_b, idx_b, total_b, dead, live in fetched:
            cur.execute(f"SELECT COUNT(*) FROM {_quote(name)}")
            live_n = int(live or 0)
            rows.append(
                {
                    "table_name": name,
                    "table_kind": table_kind(name),
                    "table_rows": int(cur.fetchone()[0]),
                    "data_size_bytes": int(data_b or 0),
                    "index_size_bytes": int(idx_b or 0),
                    "total_size_bytes": int(total_b or 0),
                    "bloat_ratio": (float(dead) / live_n) if live_n else 0.0,
                }
            )
    return rows


def _server_health() -> dict:
    probes = {
        "disk_space_bytes": "SELECT pg_database_size(current_database())",
        "active_connections": (
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname = current_database() AND state = 'active'"
        ),
        "cache_hit_ratio": """
            SELECT sum(blks_hit)::float8
                   / NULLIF(sum(blks_hit) + sum(blks_read), 0)
            FROM pg_stat_database
            WHERE datname = current_database()
        """,
    }
    health: dict = {}
    with _cursor() as cur:
        for key, statement in probes.items():
            cur.execute(statement)
            value = cur.fetchone()[0]
            if value is None:
                continue
            health[key] = float(value) if key == "cache_hit_ratio" else int(value)
    return health


def _record_selects(replay: Callable[[], object]) -> list[str]:
    from django.conf import settings
    from django.db import connections, reset_queries

    alias = ln.Record.objects.db
    previous = settings.DEBUG
    settings.DEBUG = True
    reset_queries()
    try:
        replay()
        logged = list(connections[alias].queries)
    except Exception:  # noqa: BLE001 — best-effort diagnostics across database drivers
        return []
    finally:
        settings.DEBUG = previous
    statements = []
    for item in logged:
        statements.extend(_select_statements(str(item.get("sql", ""))))
    return statements


def _select_statements(sql: str) -> list[str]:
    """Keep all data SELECTs, excluding the authentication token call.

    Django logs the RLS `set_token` call in the same string as the lookup.
    Explaining that compound string only plans the token call, which is a
    constant Result and hides whether the lookup used an index.
    """
    statements = []
    for part in sqlparse.split(sql):
        statement = part.strip()
        head = statement[:16].upper()
        if not head.startswith("SELECT") or re.match(
            r'^SELECT\s+(?:[\w"]+\.)?"?set_token"?\s*\(',
            statement,
            re.IGNORECASE,
        ):
            continue
        statements.append(statement.rstrip(";"))
    return statements


def _explain_sql(sql: str) -> dict | None:
    statement = sql.strip().rstrip(";").replace("%", "%%")
    try:
        with _cursor() as cur:
            cur.execute(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {statement}")
            raw = cur.fetchone()[0]
    except Exception:  # noqa: BLE001 — best-effort diagnostics across database drivers
        return None
    return _plan_root(raw)


def _explain_queryset(query_name: str, uids: list[str]) -> dict | None:
    if not uids:
        return None
    query = (
        ln.Record.filter(uid__in=uids)
        if query_name == "uid_batch_with_features"
        else ln.Record.filter(uid=uids[0])
    )
    try:
        raw = query.explain(analyze=True, buffers=True, format="json")
    except Exception:  # noqa: BLE001 — best-effort diagnostics across database drivers
        return None
    return _plan_root(raw)


def _plan_root(raw: object) -> dict | None:
    if isinstance(raw, bytes | memoryview):
        raw = bytes(raw).decode()
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return None
    node = payload[0] if isinstance(payload, list) else payload
    return node if isinstance(node, dict) else None


def _aggregate(plans: list[dict], source: str) -> dict:
    if not plans:
        raise ValueError("no EXPLAIN ANALYZE plan was available")
    text = json.dumps(plans)
    execution = [
        float(plan["Execution Time"])
        for plan in plans
        if _is_number(plan.get("Execution Time"))
    ]
    planning = [
        float(plan["Planning Time"])
        for plan in plans
        if _is_number(plan.get("Planning Time"))
    ]
    buffers = {
        # Root counters include child nodes; summing the tree double-counts I/O.
        key: sum(int(plan.get("Plan", {}).get(field, 0)) for plan in plans)
        for key, field in BUFFER_FIELDS.items()
    }
    return {
        "execution_time_ms": round(sum(execution), 3) if execution else None,
        "planning_time_ms": round(sum(planning), 3) if planning else None,
        "used_index": "Index Scan" in text or "Index Only Scan" in text,
        "used_seq_scan": "Seq Scan" in text,
        "n_statements": len(plans),
        "plan_source": source,
        "query_plan": text[:PLAN_LIMIT],
        **buffers,
    }


def _cursor():
    """Use LaminDB's connection so RLS `set_token` is applied.

    A fresh psycopg connection has no JWT, and row counts on the load
    instance then fail inside `check_access()`.
    """
    from django.db import connections

    return connections[ln.Record.objects.db].cursor()


def _quote(name: str) -> str:
    if not _IDENT.fullmatch(name):
        raise ValueError(f"unexpected table name: {name!r}")
    return f'"{name}"'


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)
