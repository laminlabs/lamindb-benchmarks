"""Indirect load via Alex's fast bulk path: build a DataFrame per batch, then
ln.Record.from_dataframe(df, type=...).save() — one bulk save instead of a per-record
Python loop. This removes the per-record ORM construction that saturated the DB CPU, so
NO parallelization is needed.

signature confirmed locally:
  ln.Record.from_dataframe(df, *, type: 'Record|str',
                           name_field='__lamindb_record_name__') -> RecordBatch
  then records.save().

UNKNOWN being tested: whether from_dataframe fans out the multi-valued genes/small_molecules
list-columns into recordrecord links. If it errors on those columns, use _insert_batch_fallback
below (scalars via from_dataframe, then one add_values pass for the multi-valued links).
"""
from __future__ import annotations
import random
from datetime import date, timedelta

import pandas as pd
import lamindb as ln

import config

NAME_FIELD = "__lamindb_record_name__"


def _rows(ctx, run_uid, start_idx, n):
    f = ctx["features"]
    lo_g, hi_g = config.GENES_PER_RECORD
    lo_m, hi_m = config.MOLECULES_PER_RECORD
    rows = []
    for i in range(start_idx, start_idx + n):
        rows.append({
            NAME_FIELD: f"intervention_{run_uid}_{i}",
            "dose": f"{round(random.uniform(0.1, 100.0), 2)} uM",
            "protocol": random.choice(config.PROTOCOLS),
            "treatment_date": date(2024, 1, 1) + timedelta(days=random.randint(0, 700)),
            "treatment_time_min": random.randint(5, 1440),
            "supplier": random.choice(ctx["suppliers"]).name,
            "genes": [g.name for g in random.sample(ctx["genes"], random.randint(lo_g, hi_g))],
            "small_molecules": [m.name for m in random.sample(ctx["molecules"], random.randint(lo_m, hi_m))],
        })
    return rows


def insert_batch(ctx, run_uid, start_idx, n, using):
    df = pd.DataFrame(_rows(ctx, run_uid, start_idx, n))
    batch = ln.Record.from_dataframe(df, type=ctx["type"])
    batch.save(using=using)
    # names are unique per (run_uid, index); fetch back the uids for the query-sample pool
    names = df[NAME_FIELD].tolist()
    return list(ln.Record.filter(name__in=names).using(using).values_list("uid", flat=True))


def _insert_batch_fallback(ctx, run_uid, start_idx, n, using):
    """Use ONLY if from_dataframe rejects the multi-valued list columns. Scalars via the
    bulk path; multi-valued genes/molecules attached per-record via add_values."""
    f = ctx["features"]
    rows = _rows(ctx, run_uid, start_idx, n)
    scalar = [{k: v for k, v in r.items() if k not in ("genes", "small_molecules")} for r in rows]
    batch = ln.Record.from_dataframe(pd.DataFrame(scalar), type=ctx["type"])
    batch.save(using=using)
    by_name = {r["__lamindb_record_name__"]: r for r in rows}
    recs = list(ln.Record.filter(name__in=list(by_name)).using(using))
    for rec in recs:
        src = by_name[rec.name]
        rec.features.add_values({
            f["genes"]: [g for g in ctx["genes"] if g.name in src["genes"]],
            f["small_molecules"]: [m for m in ctx["molecules"] if m.name in src["small_molecules"]],
        })
    return [r.uid for r in recs]