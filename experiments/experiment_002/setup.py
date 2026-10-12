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
SUPPLIERS = [f"supplier_{i:02d}" for i in range(10)]
GENES = [f"gene_{i:03d}" for i in range(100)]
MOLECULES = [f"mol_{i:02d}" for i in range(20)]
PROTOCOLS = ["protocol_A", "protocol_B", "protocol_C", "protocol_D"]
GENES_PER_RECORD = (3, 5)
MOLECULES_PER_RECORD = (1, 3)


def main() -> None:
    n_rows = require_int(read_config(), "n_rows", minimum=1)
    ln.connect(instance_from_env(LOAD_INSTANCE_ENV, LOAD_INSTANCE))
    # Load-instance finish fails while its Storage row is missing.
    # ln.track(params={"n_rows": n_rows})
    created = create_interventions(n_rows)
    print(f"setup created {created} intervention records")
    # ln.finish()


def create_interventions(n_rows: int) -> int:
    intervention, features, pools = ensure_intervention_sheet()
    from observe import EVENTS_TABLE, count_rows

    before = count_rows(EVENTS_TABLE)
    started = time.perf_counter()
    created = _insert_rows(intervention, features, pools, n_rows)
    elapsed = time.perf_counter() - started
    after = count_rows(EVENTS_TABLE)
    _write_insert(n_rows, created, elapsed, before, after)
    return created


def _insert_rows(
    intervention: ln.Record,
    features: dict[str, ln.Feature],
    pools: dict,
    n_rows: int,
) -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    created = 0
    while created < n_rows:
        count = min(BATCH_SIZE, n_rows - created)
        rows = [
            _row(pools, f"{NAME_PREFIX}_{stamp}_{created + offset:05d}")
            for offset in range(count)
        ]
        _save_batch(intervention, features, pools, rows)
        created += count
    return created


def _write_insert(
    n_rows: int,
    created: int,
    elapsed: float,
    before: int | None,
    after: int | None,
) -> None:
    features: dict = {
        "measurement_kind": "insert",
        "query_name": "insert",
        "name_prefix": f"{NAME_PREFIX}_",
        "n_rows": n_rows,
        "n_records": created,
        "insert_seconds": round(elapsed, 6),
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


def ensure_intervention_sheet() -> tuple[ln.Record, dict[str, ln.Feature], dict]:
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
        "genes": ensure_pool(GENES, gene_type),
        "molecules": ensure_pool(MOLECULES, molecule_type),
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


def _row(pools: dict, name: str) -> dict:
    lo_g, hi_g = GENES_PER_RECORD
    lo_m, hi_m = MOLECULES_PER_RECORD
    treated = date(2024, 1, 1) + timedelta(days=random.randint(0, 700))
    return {
        NAME_FIELD: name,
        "dose": f"{round(random.uniform(0.1, 100.0), 2)} uM",
        "protocol": random.choice(PROTOCOLS),
        "treatment_date": treated,
        "treatment_time_min": random.randint(5, 1440),
        "supplier": random.choice(pools["suppliers"]).name,
        "genes": [
            gene.name
            for gene in random.sample(pools["genes"], random.randint(lo_g, hi_g))
        ],
        "small_molecules": [
            molecule.name
            for molecule in random.sample(pools["molecules"], random.randint(lo_m, hi_m))
        ],
    }


def _save_batch(
    intervention: ln.Record,
    features: dict[str, ln.Feature],
    pools: dict,
    rows: list[dict],
) -> None:
    frame = pd.DataFrame(rows)
    try:
        ln.Record.from_dataframe(frame, type=intervention).save()
        return
    except Exception as error:
        names = [row[NAME_FIELD] for row in rows]
        if ln.Record.filter(name__in=names).exists():
            sys.exit(f"bulk save failed after creating records: {error}")
        print(
            f"[warn] bulk save rejected list columns ({error}); "
            "linking features separately"
        )
    scalar = frame.drop(columns=["genes", "small_molecules"])
    ln.Record.from_dataframe(scalar, type=intervention).save()
    genes = {record.name: record for record in pools["genes"]}
    molecules = {record.name: record for record in pools["molecules"]}
    saved = {
        record.name: record
        for record in ln.Record.filter(name__in=[row[NAME_FIELD] for row in rows])
    }
    for row in rows:
        record = saved.get(row[NAME_FIELD])
        if record is None:
            sys.exit(f"saved intervention {row[NAME_FIELD]!r} was not found")
        record.features.add_values(
            {
                features["genes"]: [genes[name] for name in row["genes"]],
                features["small_molecules"]: [
                    molecules[name] for name in row["small_molecules"]
                ],
            }
        )


if __name__ == "__main__":
    main()
