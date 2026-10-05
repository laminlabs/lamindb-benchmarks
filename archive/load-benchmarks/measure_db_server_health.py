"""Read-only instrument for the LOAD instance. Query ladder via .using(LOAD); storage/health
via direct psycopg to LOAD_DB_URI. Option A: cross-instance feature reads can fail — caught,
null latency recorded, EXPLAIN plan/exec-time still captured so the ramp continues.

Alex fix: n_rows no longer leaks the reltuples '-1 = never analyzed' sentinel. We ANALYZE all
public tables before reading, and coerce any remaining negative estimate to 0.
"""
from __future__ import annotations
import json
import sys
import time

import config

_pg = None
LINK_TABLES = {"lamindb_recordulabel", "lamindb_recordrecord"}
DATA_TABLES = {"lamindb_recordjson"}


def _conn():
    global _pg
    if _pg is None or getattr(_pg, "closed", False):
        if not config.LOAD_DB_URI:
            sys.exit("set LAMIN_LOAD_DB_URI to the LOAD instance Postgres URI")
        try:
            import psycopg
            _pg = psycopg.connect(config.LOAD_DB_URI, autocommit=True)
        except ImportError:
            import psycopg2
            _pg = psycopg2.connect(config.LOAD_DB_URI); _pg.autocommit = True
    return _pg


def table_kind(name):
    return "link" if name in LINK_TABLES else "data" if name in DATA_TABLES else "base"


def refresh_stats(tables=None):
    """ANALYZE so reltuples estimates are populated. Default: ALL public tables, so no table
    reports the -1 'never analyzed' sentinel (Alex's question)."""
    with _conn().cursor() as cur:
        if tables is None:
            cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public';")
            tables = [r[0] for r in cur.fetchall()]
        for t in tables:
            try:
                cur.execute(f'ANALYZE "{t}";')
            except Exception:
                pass


def estimate_rows(table_name):
    with _conn().cursor() as cur:
        cur.execute("SELECT reltuples::bigint FROM pg_class WHERE relname = %s;", (table_name,))
        row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None and row[0] >= 0 else None


def _queryset(query_type, uids, using):
    import lamindb as ln
    qs = ln.Record.filter(uid__in=uids) if query_type == "uid_batch20_with_features" \
        else ln.Record.filter(uid=uids[0])
    return qs.using(using)


def _run_once(query_type, uids, using):
    qs = _queryset(query_type, uids, using)
    t0 = time.perf_counter()
    try:
        qs.one_or_none() if query_type == "uid_lookup" else qs.to_dataframe(include="features")
    except Exception:
        return None
    return (time.perf_counter() - t0) * 1000.0


def _explain(query_type, uids, using):
    try:
        plan = _queryset(query_type, uids, using).explain(analyze=True, format="json")
        obj = json.loads(plan) if isinstance(plan, str) else plan
        node = obj[0] if isinstance(obj, list) else obj
        text = json.dumps(obj)
        return (text, "Seq Scan" in text,
                "Index Scan" in text or "Index Only Scan" in text,
                node.get("Planning Time"), node.get("Execution Time"))
    except Exception:
        return (None, None, None, None, None)


def run_query_ladder(sampled_uids, using):
    rows = []
    for spec in config.QUERY_LADDER:
        qt, uids = spec["query_type"], sampled_uids[spec["query_type"]]
        lat = [_run_once(qt, uids, using) for _ in range(config.N_REPETITIONS)]
        plan, seq, idx, plan_ms, exec_ms = _explain(qt, uids, using)

        cold = [x for x in lat[:1] if x is not None]
        warm = [x for x in lat[1:] if x is not None] or cold
        for cache_state, sample in (("cold", cold), ("warm", warm)):
            s = sorted(sample)
            stats = {} if not s else {
                "latency_median_ms": s[len(s) // 2],
                "latency_p95_ms": s[min(int(len(s) * 0.95), len(s) - 1)],
                "latency_min_ms": s[0], "latency_max_ms": s[-1],
            }
            rows.append({
                "query_type": qt, "cache_state": cache_state, "n_repetitions": len(s),
                "latencies_ms": [round(x, 3) for x in sample],   # list[float] now (Alex)
                "execution_time_ms": exec_ms, "planning_time_ms": plan_ms,
                "rows_returned": len(uids),
                "used_index": idx, "used_seq_scan": seq, "query_plan": plan, **stats,
            })
    return rows


def get_data_distribution():
    """Renamed from get_storage_by_table (Alex: avoid clash with S3 'Storage').
    n_rows coerces the reltuples -1 sentinel to 0."""
    sql = """
    SELECT c.relname, c.reltuples::bigint,
           pg_relation_size(c.oid), pg_indexes_size(c.oid), pg_total_relation_size(c.oid),
           COALESCE(s.n_dead_tup,0), COALESCE(s.n_live_tup,0)
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    LEFT JOIN pg_stat_user_tables s ON s.relid=c.oid
    WHERE c.relkind='r' AND n.nspname='public'
    ORDER BY pg_total_relation_size(c.oid) DESC;
    """
    rows = []
    with _conn().cursor() as cur:
        cur.execute(sql)
        for name, n, data_b, idx_b, total_b, dead, live in cur.fetchall():
            n_rows = int(n) if n is not None and n >= 0 else 0     # -1 sentinel -> 0 (Alex)
            rows.append({
                "table_name": name, "table_kind": table_kind(name), "n_rows": n_rows,
                "data_size_bytes": int(data_b or 0), "index_size_bytes": int(idx_b or 0),
                "total_size_bytes": int(total_b or 0),
                "bloat_ratio": (dead / live) if live else 0.0,
            })
    return rows


def get_server_health():
    health = {"disk_space_bytes": None, "active_connections": None, "cache_hit_ratio": None}
    probes = {
        "disk_space_bytes": "SELECT pg_database_size(current_database());",
        "active_connections": "SELECT count(*) FROM pg_stat_activity WHERE state='active';",
        "cache_hit_ratio": """SELECT sum(blks_hit)::float
                              / NULLIF(sum(blks_hit)+sum(blks_read),0) FROM pg_stat_database;""",
    }
    with _conn().cursor() as cur:
        for key, q in probes.items():
            try:
                cur.execute(q); health[key] = cur.fetchone()[0]
            except Exception:
                pass
    return health