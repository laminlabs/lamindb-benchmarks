"""10-row smoke test: all features (incl. multi-valued) in the constructor, single
bulk save via using=. Tests the Bug C bulk-multivalued fix end to end.
Run:  python smoke.py"""
from __future__ import annotations
import random
from datetime import date, timedelta

import lamindb as ln
from lamindb.models.sqlrecord import _ensure_lamindb_router

import config
import schema


def main():
    _ensure_lamindb_router()
    ln.connect(config.TRACKING_INSTANCE)
    print("registering metric sheets on tracking...")
    schema.setup_metric_sheets()

    print(f"registering Intervention sheets on {config.LOAD_INSTANCE} via using=...")
    ctx = schema.setup_load_sheets(using=config.LOAD_INSTANCE)
    f = ctx["features"]
    lo_g, hi_g = config.GENES_PER_RECORD
    lo_m, hi_m = config.MOLECULES_PER_RECORD

    print("writing 10 Intervention rows (all features in one bulk save)...")
    records = []
    for i in range(10):
        records.append(ln.Record(
            name=f"smoke_intervention_{i}", type=ctx["type"],
            features={
                f["dose"]: f"{round(random.uniform(0.1, 100), 2)} uM",
                f["protocol"]: random.choice(config.PROTOCOLS),
                f["treatment_date"]: date(2024, 1, 1) + timedelta(days=random.randint(0, 700)),
                f["treatment_time_min"]: random.randint(5, 1440),
                f["supplier"]: random.choice(ctx["suppliers"]),
                f["genes"]: random.sample(ctx["genes"], random.randint(lo_g, hi_g)),
                f["small_molecules"]: random.sample(ctx["molecules"], random.randint(lo_m, hi_m)),
            },
        ))
    ln.save(records, using=config.LOAD_INSTANCE)

    print(f"done. wrote {len(records)} interventions to {config.LOAD_INSTANCE}.")
    print("check: recordulabel/recordrecord grew past 10 (multi-valued links).")


if __name__ == "__main__":
    main()