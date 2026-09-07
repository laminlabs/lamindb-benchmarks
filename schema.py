"""Sheet + schema registration. Incorporates Alex's review:
  - dropped `strategy` (always indirect)
  - `timestamp` is a real datetime dtype (store datetime objects, not isoformat strings)
  - `latencies_ms` is list[float] (not a json string)
  - StorageByTable -> DataDistributionAcrossDB (avoid clash with S3 "Storage")
  - a dedicated `Experiment` type + relational `experiment` feature on every metric sheet
    (integrity + cross-sheet navigation in the UI) — see note below on Alex's ambiguity
  - `experiment_id` string kept too (Alex OK'd it; analyze groups by it)

NOTE on the relational design: Alex said "experiment feature with dtype QueryMeasurement"
and "rename QueryMeasurement to Experiments", but QueryMeasurement has many rows per run,
so linking a snapshot to "a QueryMeasurement" is ill-defined. The coherent version of his
intent is a dedicated `Experiment` type (one record per run) that all sheets link to. Built
that way here — confirm the naming with Alex.

Metric schemas use minimal_set=False so sparse/optional metric columns may be absent.
Intervention keeps minimal_set=True — every intervention has all its features.
"""
from __future__ import annotations
from datetime import datetime

import lamindb as ln

import config


def _get_pool(names, type_rec, using):
    existing = {r.name: r for r in ln.Record.filter(type=type_rec).using(using)}
    out = []
    for nm in names:
        r = existing.get(nm)
        if r is None:
            r = ln.Record(name=nm, type=type_rec).save(using=using)
        out.append(r)
    return out


def setup_load_sheets(using: str) -> dict:
    """PARENT/only creator. Write-first (idempotent by name; establishes the connection)."""
    intervention = ln.Record(name="Intervention", is_type=True).save(using=using)
    supplier_t = ln.Record(name="Supplier", is_type=True).save(using=using)
    molecule_t = ln.Record(name="SmallMolecule", is_type=True).save(using=using)
    gene_t = ln.Record(name="Gene", is_type=True).save(using=using)

    feats = {
        "dose": ln.Feature(name="dose", dtype=str).save(using=using),
        "protocol": ln.Feature(name="protocol", dtype=str).save(using=using),
        "treatment_date": ln.Feature(name="treatment_date", dtype=datetime).save(using=using),
        "treatment_time_min": ln.Feature(name="treatment_time_min", dtype=int).save(using=using),
        "supplier": ln.Feature(name="supplier", dtype=supplier_t).save(using=using),
        "genes": ln.Feature(name="genes", dtype=gene_t).save(using=using),
        "small_molecules": ln.Feature(name="small_molecules", dtype=molecule_t).save(using=using),
    }
    schema = ln.Schema(name="intervention_schema", features=list(feats.values()),
                       minimal_set=True).save(using=using)
    intervention.schema = schema
    intervention.save(using=using)

    pools = {
        "suppliers": _get_pool(config.SUPPLIERS, supplier_t, using),
        "genes": _get_pool(config.GENES, gene_t, using),
        "molecules": _get_pool(config.SMALL_MOLECULES, molecule_t, using),
    }
    return {"type": intervention, "features": feats, **pools}


# ---------- tracking-side metric sheets ----------

NULLABLE = {
    "latency_median_ms", "latency_p95_ms", "latency_min_ms", "latency_max_ms",
    "execution_time_ms", "planning_time_ms",
    "disk_space_bytes", "cache_hit_ratio", "active_connections",
    "insert_seconds", "insert_records_per_sec", "write_events_per_record",
    "latencies_ms",
}

# `strategy` dropped. `timestamp` -> datetime. `experiment` (relational) added per-sheet below.
_COMMON = {"experiment_id": str, "rls_on": bool, "scale_step": int, "timestamp": datetime}

RUN_SNAPSHOT = {**_COMMON,
    "n_records": int, "n_events": int, "n_auxiliary_entries": int, "n_total_rows": int,
    "insert_seconds": float, "insert_records_per_sec": float, "write_events_per_record": float,
    "disk_space_bytes": int, "cache_hit_ratio": float, "active_connections": int}

QUERY_MEASUREMENT = {**_COMMON,
    "query_type": str, "cache_state": str, "n_repetitions": int,
    "latency_median_ms": float, "latency_p95_ms": float,
    "latency_min_ms": float, "latency_max_ms": float,
    "execution_time_ms": float, "planning_time_ms": float, "rows_returned": int,
    "used_index": bool, "used_seq_scan": bool, "latencies_ms": list[float], "query_plan": str}

DATA_DISTRIBUTION = {**_COMMON,
    "table_name": str, "table_kind": str,
    "n_rows": int, "data_size_bytes": int, "index_size_bytes": int,
    "total_size_bytes": int, "bloat_ratio": float}


def _features(spec, experiment_t):
    feats = {}
    for name, dtype in spec.items():
        feats[name] = ln.Feature(name=name, dtype=dtype, nullable=(name in NULLABLE)).save()
    # relational link to the Experiment record (integrity + cross-sheet navigation)
    feats["experiment"] = ln.Feature(name="experiment", dtype=experiment_t).save()
    return feats


def setup_metric_sheets() -> dict:
    experiment_t = ln.Record(name="Experiment", is_type=True).save()   # one record per run
    out = {"_experiment_type": experiment_t}
    for name, spec in (("RunSnapshot", RUN_SNAPSHOT),
                       ("QueryMeasurement", QUERY_MEASUREMENT),
                       ("DataDistributionAcrossDB", DATA_DISTRIBUTION)):
        type_rec = ln.Record(name=name, is_type=True).save()
        feats = _features(spec, experiment_t)
        schema = ln.Schema(name=f"{name.lower()}_schema", features=list(feats.values()),
                           minimal_set=False).save()
        type_rec.schema = schema
        type_rec.save()
        out[name] = (type_rec, feats)
    return out