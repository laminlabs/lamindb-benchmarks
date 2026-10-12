"""Add and remove the to_one column and its partial unique index.

A wipe truncates rows. It does not drop columns or indexes. Drop both before
the process exits, including on failure, and again at the start of the next
run. DDL uses an autocommit root connection, then reconnects as the load user.
"""
from __future__ import annotations

import re
import time

import lamindb as ln

TABLE = "lamindb_recordrecord"
COLUMN = "to_one"
INDEX_NAME = "unique_recordrecord_to_one"
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def drop_leftovers(instance: str) -> None:
    """Remove this experiment's index and column if a previous run left them."""
    _as_root(
        instance,
        [
            (f"DROP INDEX IF EXISTS {_qualified(INDEX_NAME)}", []),
            (
                f"ALTER TABLE {_qualified(TABLE)} "
                f"DROP COLUMN IF EXISTS {_quote(COLUMN)}",
                [],
            ),
        ],
    )


def add_column(instance: str) -> float:
    started = time.perf_counter()
    _as_root(
        instance,
        [
            (
                f"ALTER TABLE {_qualified(TABLE)} "
                f"ADD COLUMN {_quote(COLUMN)} boolean NOT NULL DEFAULT false",
                [],
            )
        ],
    )
    return time.perf_counter() - started


def create_index(instance: str) -> float:
    started = time.perf_counter()
    _as_root(
        instance,
        [
            (
                # Postgres rejects a schema on the index name. The index is created
                # in the table's schema when the table is schema-qualified.
                f"CREATE UNIQUE INDEX {_quote(INDEX_NAME)} "
                f"ON {_qualified(TABLE)} (record_id, feature_id) "
                f"WHERE {_quote(COLUMN)}",
                [],
            )
        ],
    )
    return time.perf_counter() - started


def relation_size(instance: str) -> dict[str, int]:
    rows = _as_root(
        instance,
        [
            (
                "SELECT pg_relation_size(CAST(%s AS regclass)), "
                "pg_indexes_size(CAST(%s AS regclass)), "
                "pg_total_relation_size(CAST(%s AS regclass))",
                [f"public.{TABLE}", f"public.{TABLE}", f"public.{TABLE}"],
            )
        ],
        fetch=True,
    )
    heap, indexes, total = rows[0]
    return {
        "heap_bytes": int(heap),
        "indexes_bytes": int(indexes),
        "total_bytes": int(total),
    }


def index_size(instance: str) -> int:
    rows = _as_root(
        instance,
        [
            (
                "SELECT pg_relation_size(CAST(%s AS regclass))",
                [f"public.{INDEX_NAME}"],
            )
        ],
        fetch=True,
    )
    return int(rows[0][0])


def _as_root(
    instance: str,
    statements: list[tuple[str, list]],
    *,
    fetch: bool = False,
) -> list[tuple]:
    ln.connect(instance, use_root_db_user=True)
    uri = ln.setup.settings.instance.db
    fetched: list[tuple] = []
    connection = None
    try:
        connection = _pg(uri)
        with connection.cursor() as cur:
            cur.execute("SET lock_timeout = '15s'")
            cur.execute("SET search_path TO public")
            for sql, params in statements:
                cur.execute(sql, params or None)
                if fetch:
                    fetched.extend(cur.fetchall())
    finally:
        try:
            if connection is not None:
                connection.close()
        finally:
            ln.connect(instance)
    return fetched


def _pg(uri: str):
    try:
        import psycopg

        return psycopg.connect(uri, autocommit=True)
    except ImportError:
        import psycopg2

        connection = psycopg2.connect(uri)
        connection.autocommit = True
        return connection


def require_postgres() -> None:
    from django.db import connections

    vendor = connections[ln.Record.objects.db].vendor
    if vendor != "postgresql":
        raise SystemExit(f"Postgres is required, got {vendor}")


def _quote(name: str) -> str:
    if not _IDENT.fullmatch(name):
        raise ValueError(f"unexpected identifier: {name!r}")
    return f'"{name}"'


def _qualified(name: str) -> str:
    return f"public.{_quote(name)}"
