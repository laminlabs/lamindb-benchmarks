"""Time RecordRecord inserts before and after the to_one unique index.

Adds the column, times inserts with to_one true, and rolls them back. Then
adds the partial unique index and times the same inserts again. One committed
batch is used only to measure the populated index, then deleted. Drops the
index and the column before exiting, including on failure.

Usage:
  python experiments/experiment_004/run.py CONFIG OUTPUT_DIRECTORY
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_HERE = Path(__file__).resolve().parent
for _path in (_ROOT, _HERE):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import lamindb as ln
from ddl import (
    COLUMN,
    INDEX_NAME,
    TABLE,
    add_column,
    create_index,
    drop_leftovers,
    index_size,
    relation_size,
    require_postgres,
)

from benchmarks.config import (
    LOAD_INSTANCE,
    LOAD_INSTANCE_ENV,
    instance_from_env,
    read_config,
    require_int,
)
from benchmarks.measurements import write_measurement

NAME = Path(__file__).resolve().parent.name
SUBJECTS = "Subjects"
SITES = "Sites"
FEATURE_NAME = "site"
# 4 parameters per row. Stay under Postgres's 65535-parameter limit.
CHUNK = 2000
DEFAULT_VALUES = 100
DEFAULT_REPETITIONS = 11


def main() -> None:
    params = read_config()
    n_rows = require_int(params, "n_rows", minimum=1)
    n_values = require_int(params, "n_values", minimum=1, default=DEFAULT_VALUES)
    repetitions = require_int(
        params, "n_repetitions", minimum=1, default=DEFAULT_REPETITIONS
    )
    instance = instance_from_env(LOAD_INSTANCE_ENV, LOAD_INSTANCE)
    ln.connect(instance)
    # ln.track()
    require_postgres()
    drop_leftovers(instance)
    feature_id = None
    try:
        feature_id, rows = link_rows()
        if len(rows) != n_rows:
            sys.exit(f"expected {n_rows} links, built {len(rows)}")
        _assert_absent()
        before = relation_size(instance)
        alter_seconds = add_column(instance)
        after_column = relation_size(instance)
        print(
            f"added {COLUMN} in {alter_seconds:.3f}s, "
            f"total {before['total_bytes']} -> {after_column['total_bytes']} bytes"
        )
        if not _column_exists():
            sys.exit(f"{COLUMN} was not added")
        without = time_inserts(feature_id, rows, repetitions, indexed=False)
        create_seconds = create_index(instance)
        empty_index_bytes = index_size(instance)
        after_index = relation_size(instance)
        print(
            f"created {INDEX_NAME} in {create_seconds:.3f}s, "
            f"empty index {empty_index_bytes} bytes"
        )
        with_index = time_inserts(feature_id, rows, repetitions, indexed=True)
        populated = measure_populated(instance, feature_id, rows)
        print(
            f"populated index {populated['index_bytes_populated']} bytes "
            f"after {len(rows)} links"
        )
        _write_size(
            n_rows,
            n_values,
            before,
            after_column,
            after_index,
            empty_index_bytes,
            populated,
            alter_seconds,
            create_seconds,
        )
        _write_timed("insert_links", without, n_rows, n_values, indexed=False)
        _write_timed(
            "insert_links_indexed", with_index, n_rows, n_values, indexed=True
        )
    finally:
        _restore(instance, feature_id)
    # ln.finish()


def link_rows() -> tuple[int, list[tuple[int, int, int, bool]]]:
    subjects = _ids(SUBJECTS, f"{NAME}_subject_")
    sites = _ids(SITES, f"{NAME}_site_")
    feature = ln.Feature.filter(name=FEATURE_NAME, is_type=False).one_or_none()
    if feature is None:
        sys.exit(f"feature {FEATURE_NAME!r} was not found")
    if not subjects or not sites:
        sys.exit("setup did not create subjects and sites")
    rows = [
        (subject_id, feature.id, sites[index % len(sites)], True)
        for index, subject_id in enumerate(subjects)
    ]
    return feature.id, rows


def time_inserts(
    feature_id: int,
    rows: list[tuple[int, int, int, bool]],
    repetitions: int,
    *,
    indexed: bool,
) -> list[tuple[float, int]]:
    """Time only the INSERT statements. Each repetition is rolled back."""
    if _index_exists() != indexed:
        state = "present" if _index_exists() else "absent"
        sys.exit(f"{INDEX_NAME} is {state}; expected indexed={indexed}")
    samples = []
    label = "indexed" if indexed else "no index"
    for repetition in range(1, repetitions + 1):
        elapsed, inserted = _insert_and_rollback(rows)
        samples.append((round(elapsed * 1000.0, 3), inserted))
        print(f"{label} rep {repetition} {elapsed * 1000.0:.3f} ms")
    _assert_empty(feature_id)
    return samples


def measure_populated(
    instance: str, feature_id: int, rows: list[tuple[int, int, int, bool]]
) -> dict[str, int]:
    """Commit one batch so the index size includes rows, then delete it."""
    _insert_committed(rows)
    try:
        size = relation_size(instance)
        return {
            "index_bytes_populated": index_size(instance),
            "heap_bytes_populated": size["heap_bytes"],
            "total_bytes_populated": size["total_bytes"],
        }
    finally:
        _delete_links(feature_id)
        _assert_empty(feature_id)


def _insert_and_rollback(
    rows: list[tuple[int, int, int, bool]],
) -> tuple[float, int]:
    from django.db import transaction

    db = ln.Record.objects.db
    with transaction.atomic(using=db):
        started = time.perf_counter()
        inserted = _insert(db, rows)
        elapsed = time.perf_counter() - started
        if inserted != len(rows):
            raise RuntimeError(f"inserted {inserted} links, expected {len(rows)}")
        transaction.set_rollback(True, using=db)
    return elapsed, inserted


def _insert_committed(rows: list[tuple[int, int, int, bool]]) -> None:
    from django.db import transaction

    db = ln.Record.objects.db
    with transaction.atomic(using=db):
        inserted = _insert(db, rows)
    if inserted != len(rows):
        raise RuntimeError(f"inserted {inserted} links, expected {len(rows)}")


def _insert(db: str, rows: list[tuple[int, int, int, bool]]) -> int:
    inserted = 0
    with _cursor(db) as cur:
        for start in range(0, len(rows), CHUNK):
            chunk = rows[start : start + CHUNK]
            placeholders = ",".join(["(%s,%s,%s,%s)"] * len(chunk))
            params = [value for row in chunk for value in row]
            cur.execute(
                f"INSERT INTO {TABLE} "
                f"(record_id, feature_id, value_id, {COLUMN}) "
                f"VALUES {placeholders}",
                params,
            )
            inserted += len(chunk) if cur.rowcount < 0 else int(cur.rowcount)
    return inserted


def _delete_links(feature_id: int) -> None:
    with _cursor() as cur:
        cur.execute(f"DELETE FROM {TABLE} WHERE feature_id = %s", [feature_id])


def _assert_empty(feature_id: int) -> None:
    count = _link_count(feature_id)
    if count != 0:
        sys.exit(f"expected no {TABLE} rows for feature {feature_id}, found {count}")


def _link_count(feature_id: int) -> int:
    with _cursor() as cur:
        cur.execute(
            f"SELECT count(*) FROM {TABLE} WHERE feature_id = %s",
            [feature_id],
        )
        return int(cur.fetchone()[0])


def _restore(instance: str, feature_id: int | None) -> None:
    delete_error = None
    if feature_id is not None:
        try:
            _delete_links(feature_id)
        except Exception as exc:
            delete_error = exc
    drop_leftovers(instance)
    if _column_exists() or _index_exists():
        sys.exit(f"failed to restore {TABLE}: {COLUMN} or {INDEX_NAME} is still present")
    if delete_error is not None:
        raise delete_error
    if feature_id is not None and _link_count(feature_id) != 0:
        sys.exit(f"failed to delete committed {TABLE} rows for feature {feature_id}")
    print(f"restored {TABLE}: dropped {INDEX_NAME} and {COLUMN}")


def _assert_absent() -> None:
    if _column_exists() or _index_exists():
        sys.exit(f"{COLUMN} or {INDEX_NAME} was still present after the initial drop")


def _column_exists() -> bool:
    with _cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_catalog.pg_attribute a "
            "JOIN pg_catalog.pg_class c ON c.oid = a.attrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relname = %s "
            "AND a.attname = %s AND a.attnum > 0 AND NOT a.attisdropped",
            [TABLE, COLUMN],
        )
        return cur.fetchone() is not None


def _index_exists() -> bool:
    with _cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relname = %s AND c.relkind = 'i'",
            [INDEX_NAME],
        )
        return cur.fetchone() is not None


def _write_timed(
    stem: str,
    samples: list[tuple[float, int]],
    n_rows: int,
    n_values: int,
    *,
    indexed: bool,
) -> None:
    written = write_measurement(
        stem,
        [
            {"repetition": index, "latency_ms": latency, "value": inserted}
            for index, (latency, inserted) in enumerate(samples, start=1)
        ],
        key=f"benchmarks/measurements/{NAME}/{stem}.parquet",
        description=(
            f"Insert {n_rows} to_one RecordRecord links "
            f"{'with' if indexed else 'without'} {INDEX_NAME}. "
            "Each repetition is rolled back. value is the inserted row count."
        ),
        features={
            "measurement_kind": "insert",
            "query_name": stem,
            "n_rows": n_rows,
            "n_values": n_values,
            "index_present": indexed,
            "n_links": n_rows,
        },
    )
    _print_written(written)


def _write_size(
    n_rows: int,
    n_values: int,
    before: dict[str, int],
    after_column: dict[str, int],
    after_index: dict[str, int],
    empty_index_bytes: int,
    populated: dict[str, int],
    alter_seconds: float,
    create_seconds: float,
) -> None:
    column_bytes = after_column["total_bytes"] - before["total_bytes"]
    written = write_measurement(
        "relation_size",
        [
            {
                "repetition": 1,
                "latency_ms": round(alter_seconds * 1000.0, 3),
                "value": column_bytes,
            }
        ],
        key=f"benchmarks/measurements/{NAME}/relation_size.parquet",
        description=(
            f"Size of {TABLE} before the column, after the column, and after "
            f"{INDEX_NAME}. value is the total-byte change from adding the column. "
            "A constant default is often metadata-only, so that change can be 0."
        ),
        features={
            "measurement_kind": "distribution",
            "query_name": "relation_size",
            "n_rows": n_rows,
            "n_values": n_values,
            "heap_bytes_before": before["heap_bytes"],
            "indexes_bytes_before": before["indexes_bytes"],
            "total_bytes_before": before["total_bytes"],
            "heap_bytes_after_column": after_column["heap_bytes"],
            "indexes_bytes_after_column": after_column["indexes_bytes"],
            "total_bytes_after_column": after_column["total_bytes"],
            "heap_bytes_after_index": after_index["heap_bytes"],
            "indexes_bytes_after_index": after_index["indexes_bytes"],
            "total_bytes_after_index": after_index["total_bytes"],
            "column_bytes": column_bytes,
            "index_bytes": empty_index_bytes,
            "index_bytes_populated": populated["index_bytes_populated"],
            "heap_bytes_populated": populated["heap_bytes_populated"],
            "total_bytes_populated": populated["total_bytes_populated"],
            "alter_ms": round(alter_seconds * 1000.0, 3),
            "create_index_ms": round(create_seconds * 1000.0, 3),
        },
    )
    _print_written(written)


def _ids(type_name: str, prefix: str) -> list[int]:
    type_record = ln.Record.filter(name=type_name, is_type=True).one_or_none()
    if type_record is None:
        sys.exit(f"record type {type_name!r} was not found")
    ids = list(
        ln.Record.filter(type=type_record, name__startswith=prefix)
        .order_by("name")
        .values_list("id", flat=True)
    )
    return [int(value) for value in ids]


def _cursor(db: str | None = None):
    from django.db import connections

    return connections[db or ln.Record.objects.db].cursor()


def _print_written(written: Path | None) -> None:
    print(
        "measurement not saved; results directory is unset"
        if written is None
        else f"measurement written: {written.name}"
    )
