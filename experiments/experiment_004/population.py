"""Create subjects, a site catalog, and one record-valued feature.

Does not insert RecordRecord links and does not add the to_one column. The
query script does both, then removes them. Drops a leftover column or index
first, because a wipe does not. Does not call ln.track(): the load instance
cannot finish while its Storage row is missing.

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
from ddl import drop_leftovers, require_postgres

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
BATCH = 2000
DEFAULT_VALUES = 100


def main() -> None:
    params = read_config()
    n_rows = require_int(params, "n_rows", minimum=1)
    n_values = require_int(params, "n_values", minimum=1, default=DEFAULT_VALUES)
    instance = instance_from_env(LOAD_INSTANCE_ENV, LOAD_INSTANCE)
    ln.connect(instance)
    # ln.track(params={"n_rows": n_rows, "n_values": n_values})
    require_postgres()
    drop_leftovers(instance)
    started = time.perf_counter()
    subjects, sites = create_sheet(n_rows, n_values)
    elapsed = time.perf_counter() - started
    _write_populate(n_rows, n_values, subjects, sites, elapsed)
    print(f"setup created {subjects} subjects and {sites} sites in {elapsed:.1f}s")
    # ln.finish()


def create_sheet(n_rows: int, n_values: int) -> tuple[int, int]:
    subjects = ensure_type(SUBJECTS)
    sites = ensure_type(SITES)
    feature = ensure_feature(FEATURE_NAME, sites)
    if subjects.schema is None:
        subjects.schema = ln.Schema(
            [feature],
            name="subjects_schema",
            minimal_set=True,
        ).save()
        subjects.save()
    _clear(subjects)
    _clear(sites)
    _insert_records(subjects, n_rows, f"{NAME}_subject_")
    _insert_records(sites, n_values, f"{NAME}_site_")
    return n_rows, n_values


def _clear(type_record: ln.Record) -> None:
    """A lone rerun must not append. The batch wipe also empties the instance."""
    ln.Record.filter(type=type_record, is_type=False).delete()


def _insert_records(type_record: ln.Record, count: int, prefix: str) -> None:
    created = 0
    for start in range(0, count, BATCH):
        size = min(BATCH, count - start)
        rows = [
            ln.Record(
                name=f"{prefix}{start + offset:06d}",
                type=type_record,
                space_id=type_record.space_id,
                branch_id=type_record.branch_id,
            )
            for offset in range(size)
        ]
        saved = ln.Record.objects.bulk_create(rows, batch_size=BATCH)
        if any(record.id is None for record in saved):
            sys.exit(f"bulk_create returned records without ids for {prefix!r}")
        created += len(saved)
    if created != count:
        sys.exit(f"bulk_create returned {created} records, expected {count}")


def _write_populate(
    n_rows: int,
    n_values: int,
    subjects: int,
    sites: int,
    elapsed: float,
) -> None:
    written = write_measurement(
        "populate",
        [{"repetition": 1, "latency_ms": round(elapsed * 1000.0, 3), "value": subjects}],
        key=f"benchmarks/measurements/{NAME}/populate.parquet",
        description=(
            f"bulk_create of {subjects} subjects and {sites} sites. "
            "No RecordRecord links yet."
        ),
        features={
            "measurement_kind": "insert",
            "query_name": "populate",
            "n_rows": n_rows,
            "n_values": n_values,
            "n_records": subjects + sites,
            "insert_seconds": round(elapsed, 6),
        },
    )
    print(
        "populate measurement not saved; results directory is unset"
        if written is None
        else f"measurement written: {written.name}"
    )


def ensure_type(name: str) -> ln.Record:
    matches = ln.Record.filter(name=name, is_type=True).to_list()
    if len(matches) > 1:
        sys.exit(f"multiple record types named {name!r}")
    if matches:
        return matches[0]
    return ln.Record(name=name, is_type=True).save()


def ensure_feature(name: str, sites: ln.Record) -> ln.Feature:
    from lamindb.models.feature import serialize_dtype

    matches = ln.Feature.filter(name=name, is_type=False).to_list()
    if len(matches) > 1:
        sys.exit(f"multiple features named {name!r}")
    expected = serialize_dtype(sites)
    if matches:
        feature = matches[0]
        if feature._dtype_str != expected:
            sys.exit(
                f"feature {name!r} has dtype {feature._dtype_str}, expected {expected}. "
                "Reset the load instance so the feature can be recreated."
            )
        return feature
    return ln.Feature(name=name, dtype=sites).save()
