"""Create intervention records on the load instance.

Parameters come from the benchmark config. The load instance is
LAMIN_LOAD_INSTANCE or laminlabs/lamindb-load-test.

Usage:
  python experiments/experiment_002/setup.py
"""
from __future__ import annotations

import random
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_HERE = Path(__file__).resolve().parent
for _path in (_ROOT, _HERE):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import lamindb as ln
import pandas as pd
from django.db import OperationalError, transaction
from workload import workload_parameters

from benchmarks.config import (
    LOAD_INSTANCE,
    LOAD_INSTANCE_ENV,
    instance_from_env,
    read_config,
    require_int,
)
from benchmarks.measurements import write_measurement

NAME_PREFIX = Path(__file__).resolve().parent.name
NAME_FIELD = "__lamindb_record_name__"
BATCH_SIZE = 1000
MAX_BATCH_ATTEMPTS = 3
# Serialization failure, deadlock, and lock timeout: safe to retry after rollback.
RETRYABLE_SQLSTATES = {"40001", "40P01", "55P03"}
SUPPLIERS = [f"supplier_{i:02d}" for i in range(10)]
PROTOCOLS = ["protocol_A", "protocol_B", "protocol_C", "protocol_D"]


def main() -> None:
    params = read_config()
    n_rows = require_int(params, "n_rows", minimum=1)
    workload = workload_parameters(params)
    ln.connect(instance_from_env(LOAD_INSTANCE_ENV, LOAD_INSTANCE))
    # Load-instance finish fails while its Storage row is missing.
    # ln.track(params={"n_rows": n_rows})
    created = create_interventions(n_rows, workload)
    print(f"setup created {created} intervention records")
    analyze_populated_tables()
    # ln.finish()


def analyze_populated_tables() -> None:
    """Refresh planner statistics after insertion, outside all benchmark timers."""
    # The JWT role used by the benchmark cannot analyze tables it does not own.
    # Switch only after all inserts and their measurements have finished. The
    # query stage starts in a fresh process using the normal benchmark role.
    target = instance_from_env(LOAD_INSTANCE_ENV, LOAD_INSTANCE)
    ln.connect(target, use_root_db_user=True)
    import psycopg
    from psycopg import sql

    # Use a dedicated maintenance connection: switching database roles resets
    # Django, so existing model/transaction objects must not be reused here.
    with (
        psycopg.connect(ln.setup.settings.instance.db, autocommit=True) as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND (tablename LIKE %s OR tablename = %s) ORDER BY tablename",
            ("lamindb_%", "hubmodule_dbwrite"),
        )
        tables = [row[0] for row in cursor.fetchall()]
        if not tables:
            raise RuntimeError("No benchmark tables found for ANALYZE")
        names = sql.SQL(", ").join(sql.Identifier("public", table) for table in tables)
        cursor.execute(sql.SQL("ANALYZE {}").format(names))
    print(f"ANALYZE completed for {len(tables)} load-instance tables")


def create_interventions(n_rows: int, workload: dict[str, int]) -> int:
    intervention, _, pools = ensure_intervention_sheet(workload)
    from observe import EVENTS_TABLE, count_rows

    before = count_rows(EVENTS_TABLE)
    batches = []
    created = _insert_rows(intervention, pools, n_rows, workload, batches)
    # Exclude serializing the batch diagnostics from population throughput.
    elapsed = sum(batch["latency_ms"] for batch in batches) / 1000
    after = count_rows(EVENTS_TABLE)
    _write_insert(n_rows, created, elapsed, before, after, workload, batches)
    return created


def _insert_rows(
    intervention: ln.Record,
    pools: dict,
    n_rows: int,
    workload: dict[str, int],
    batches: list[dict],
) -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    rng = random.Random(workload["random_seed"])
    created = 0
    try:
        while created < n_rows:
            count = min(BATCH_SIZE, n_rows - created)
            batch_started = time.perf_counter()
            rows = [
                _row(pools, f"{NAME_PREFIX}_{stamp}_{created + offset:05d}", rng, workload)
                for offset in range(count)
            ]
            attempts = []
            succeeded = False
            try:
                _save_batch(intervention, rows, attempts)
                succeeded = True
            finally:
                batch_seconds = time.perf_counter() - batch_started
                batches.append({
                    "repetition": len(batches) + 1,
                    "latency_ms": round(batch_seconds * 1000, 3),
                    "value": count if succeeded else 0,
                    "batch_index": len(batches) + 1,
                    "batch_size": count,
                    "records_before": created,
                    "records_after": created + count if succeeded else created,
                    "retry_count": max(len(attempts) - 1, 0),
                    "attempt_count": len(attempts),
                    "save_ms": round(sum(a["save_ms"] for a in attempts), 3),
                    "retry_wait_ms": sum(a["retry_wait_ms"] for a in attempts),
                    "succeeded": succeeded,
                    "sqlstate": attempts[-1]["sqlstate"] if attempts else "",
                })
            created += count
    finally:
        # The runner uploads these diagnostics even if a batch exhausts retries.
        if batches:
            write_measurement(
                "insert_batches", batches,
                columns=tuple(batches[0]),
                key=f"benchmarks/measurements/{NAME_PREFIX}/insert_batches.parquet",
                description="Per-batch population timings, retries, and outcomes",
                features={
                    "measurement_kind": "insert_batches",
                    "query_name": "insert_batches",
                    "n_rows": n_rows,
                    "batch_size": BATCH_SIZE,
                    "insert_retry_count": sum(b["retry_count"] for b in batches),
                    **workload,
                },
            )
    return created


def _write_insert(
    n_rows: int,
    created: int,
    elapsed: float,
    before: int | None,
    after: int | None,
    workload: dict[str, int],
    batches: list[dict],
) -> None:
    features: dict = {
        "measurement_kind": "insert",
        "query_name": "insert",
        "name_prefix": f"{NAME_PREFIX}_",
        "n_rows": n_rows,
        "n_records": created,
        "insert_seconds": round(elapsed, 6),
        "insert_retry_count": sum(b["retry_count"] for b in batches),
        "insert_batch_count": len(batches),
        **workload,
    }
    if elapsed > 0:
        features["insert_records_per_sec"] = round(created / elapsed, 3)
    if before is not None and after is not None and created:
        n_events = max(after - before, 0)
        features["n_events"] = n_events
        features["write_events_per_record"] = round(n_events / created, 6)
    written = write_measurement(
        "insert",
        [{"repetition": 1, "latency_ms": round(elapsed * 1000.0, 3), "value": created}],
        key=f"benchmarks/measurements/{NAME_PREFIX}/insert.parquet",
        description=(
            f"Timed insert of {created} intervention records "
            f"named {NAME_PREFIX}_*"
        ),
        features=features,
    )
    rate = features.get("insert_records_per_sec")
    print(
        f"insert {created} records in {elapsed:.3f}s"
        + (f" ({rate:.1f}/s)" if rate is not None else "")
    )
    print(
        "insert measurement not saved; results directory is unset"
        if written is None
        else f"measurement written: {written.name}"
    )


def ensure_intervention_sheet(
    workload: dict[str, int],
) -> tuple[ln.Record, dict[str, ln.Feature], dict]:
    supplier_type = ensure_type("Supplier")
    gene_type = ensure_type("Gene")
    molecule_type = ensure_type("SmallMolecule")
    intervention = ensure_type("Intervention")
    features = {
        "dose": ensure_feature("dose", str),
        "protocol": ensure_feature("protocol", str),
        "treatment_date": ensure_feature("treatment_date", datetime),
        "treatment_time_min": ensure_feature("treatment_time_min", int),
        "supplier": ensure_feature("supplier", supplier_type),
        "genes": ensure_feature("genes", gene_type),
        "small_molecules": ensure_feature("small_molecules", molecule_type),
    }
    if intervention.schema is None:
        intervention.schema = ln.Schema(
            list(features.values()),
            name="intervention_schema",
            minimal_set=True,
        ).save()
        intervention.save()
    pools = {
        "suppliers": ensure_pool(SUPPLIERS, supplier_type),
        "genes": ensure_pool(
            [f"gene_{i:03d}" for i in range(workload["n_genes"])], gene_type,
        ),
        "molecules": ensure_pool(
            [f"mol_{i:02d}" for i in range(workload["n_molecules"])], molecule_type,
        ),
    }
    return intervention, features, pools


def ensure_type(name: str) -> ln.Record:
    matches = ln.Record.filter(name=name, is_type=True).to_list()
    if len(matches) > 1:
        sys.exit(f"multiple record types named {name!r}")
    if matches:
        return matches[0]
    return ln.Record(name=name, is_type=True).save()


def ensure_feature(name: str, dtype: object) -> ln.Feature:
    from lamindb.models.feature import serialize_dtype

    matches = ln.Feature.filter(name=name, is_type=False).to_list()
    if len(matches) > 1:
        sys.exit(f"multiple features named {name!r}")
    expected = serialize_dtype(dtype)
    if matches:
        feature = matches[0]
        if feature._dtype_str != expected:
            sys.exit(
                f"feature {name!r} has dtype {feature._dtype_str}, expected {expected}. "
                "Reset the load instance so the feature can be recreated."
            )
        return feature
    return ln.Feature(name=name, dtype=dtype).save()


def ensure_pool(names: list[str], type_record: ln.Record) -> list[ln.Record]:
    existing = {
        record.name: record
        for record in ln.Record.filter(type=type_record, is_type=False)
    }
    records = []
    missing = []
    for name in names:
        record = existing.get(name)
        if record is None:
            record = ln.Record(name=name, type=type_record)
            missing.append(record)
        records.append(record)
    if missing:
        ln.save(missing)
    return records


def _row(pools: dict, name: str, rng: random.Random, workload: dict[str, int]) -> dict:
    lo_g, hi_g = workload["genes_per_record_min"], workload["genes_per_record_max"]
    lo_m = workload["small_molecules_per_record_min"]
    hi_m = workload["small_molecules_per_record_max"]
    treated = date(2024, 1, 1) + timedelta(days=rng.randint(0, 700))
    return {
        NAME_FIELD: name,
        "dose": f"{round(rng.uniform(0.1, 100.0), 2)} uM",
        "protocol": rng.choice(PROTOCOLS),
        "treatment_date": treated,
        "treatment_time_min": rng.randint(5, 1440),
        "supplier": rng.choice(pools["suppliers"]).name,
        "genes": [
            gene.name
            for gene in rng.sample(pools["genes"], rng.randint(lo_g, hi_g))
        ],
        "small_molecules": [
            molecule.name
            for molecule in rng.sample(pools["molecules"], rng.randint(lo_m, hi_m))
        ],
    }


def _save_batch(
    intervention: ln.Record,
    rows: list[dict],
    attempts: list[dict],
) -> None:
    frame = pd.DataFrame(rows)
    for attempt in range(1, MAX_BATCH_ATTEMPTS + 1):
        started = time.perf_counter()
        retry_delay = 0
        sqlstate = ""
        try:
            with transaction.atomic(using=intervention._state.db):
                ln.Record.from_dataframe(frame, type=intervention).save()
            return
        except OperationalError as error:
            cause = error.__cause__
            sqlstate = getattr(cause, "sqlstate", None) or getattr(cause, "pgcode", None) or ""
            if sqlstate not in RETRYABLE_SQLSTATES or attempt == MAX_BATCH_ATTEMPTS:
                raise
            retry_delay = 2 ** (attempt - 1)
            print(
                f"[warn] batch save failed ({sqlstate}); "
                f"retrying in {retry_delay}s (attempt {attempt + 1}/{MAX_BATCH_ATTEMPTS})"
            )
        finally:
            attempts.append({
                "save_ms": (time.perf_counter() - started) * 1000,
                "retry_wait_ms": retry_delay * 1000,
                "sqlstate": sqlstate,
            })
        if retry_delay:
            time.sleep(retry_delay)


if __name__ == "__main__":
    main()
