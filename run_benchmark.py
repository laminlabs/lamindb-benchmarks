from __future__ import annotations
import random
import time
import uuid
from datetime import datetime, timezone

import pandas as pd

import lamindb as ln
from lamindb.models.sqlrecord import _ensure_lamindb_router

import config
import populate
import schema
import measure_db_server_health as measure

LOAD = config.LOAD_INSTANCE
TRACKING = config.TRACKING_INSTANCE


def _save_metric(type_rec, feats, name, values):
    rec = ln.Record(name=name, type=type_rec).save()
    clean = {}
    for k, feat in feats.items():
        v = values.get(k, None)
        if v is None:
            continue
        try:
            if pd.isna(v):        # scalars only; lists/records pass through
                continue
        except (TypeError, ValueError):
            pass
        clean[feat] = v
    if clean:
        rec.features.add_values(clean)
    return rec


def _sample_uids(ctx, pool):
    if len(pool) < 20:
        pool = list(ln.Record.filter(type=ctx["type"]).using(LOAD)
                    .order_by("-created_at").values_list("uid", flat=True)[:config.UID_POOL_SIZE])
    return {
        "uid_lookup": random.sample(pool, 1),
        "uid_with_features": random.sample(pool, 1),
        "uid_batch20_with_features": random.sample(pool, min(20, len(pool))),
    }


def main():
    _ensure_lamindb_router()
    experiment_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:6]
    now = lambda: datetime.now(timezone.utc).replace(tzinfo=None)

    ln.connect(TRACKING)
    try:
        ln.track()
    except Exception as e:
        print(f"[warn] ln.track() failed, continuing: {e}")

    metrics = schema.setup_metric_sheets()
    ctx = schema.setup_load_sheets(using=LOAD)
    experiment_t = metrics["_experiment_type"]
    experiment = ln.Record(name=experiment_id, type=experiment_t).save()   # one record per run
    snap_t, snap_f = metrics["RunSnapshot"]
    q_t, q_f = metrics["QueryMeasurement"]
    dd_t, dd_f = metrics["DataDistributionAcrossDB"]

    inserted = ln.Record.filter(type=ctx["type"]).using(LOAD).count()
    print(f"starting from {inserted} existing interventions (single writer, from_dataframe)")
    uid_pool: list[str] = []

    for step in config.SCALE_STEPS:
        last_ms = last_n = None
        while inserted < step:
            n = min(config.BATCH_SIZE, step - inserted)
            t0 = time.perf_counter()
            uid_pool.extend(populate.insert_batch(ctx, experiment_id, inserted, n, using=LOAD))
            uid_pool = uid_pool[-config.UID_POOL_SIZE:]
            last_ms, last_n = (time.perf_counter() - t0) * 1000.0, n
            inserted += n

        measure.refresh_stats()                       # ANALYZE all tables (no -1 sentinel)
        storage = measure.get_data_distribution()
        health = measure.get_server_health()
        n_events = measure.estimate_rows("hubmodule_dbwrite")
        n_aux = sum(r["n_rows"] for r in storage if r["table_kind"] in ("link", "data"))
        qrows = measure.run_query_ladder(_sample_uids(ctx, uid_pool), using=LOAD)

        stamp = {"experiment": experiment, "experiment_id": experiment_id,
                 "rls_on": config.RLS_ON, "scale_step": step, "timestamp": now()}

        _save_metric(snap_t, snap_f, f"{experiment_id}_snap_{step}", {
            **stamp, "n_records": inserted, "n_events": n_events, "n_auxiliary_entries": n_aux,
            "n_total_rows": sum(r["n_rows"] for r in storage),
            "insert_seconds": (last_ms / 1000.0) if last_ms else None,
            "insert_records_per_sec": (last_n / (last_ms / 1000.0)) if last_ms else None,
            "write_events_per_record": (n_events / inserted) if n_events and inserted else None,
            **health,
        })
        for r in qrows:
            _save_metric(q_t, q_f, f"{experiment_id}_q_{step}_{r['query_type']}_{r['cache_state']}", {**stamp, **r})
        for r in storage:
            _save_metric(dd_t, dd_f, f"{experiment_id}_dd_{step}_{r['table_name']}", {**stamp, **r})
        print(f"[{step}] records={inserted} events={n_events} "
              f"ins={(last_n/(last_ms/1000.0)) if last_ms else 0:.0f}/s "
              f"cache_hit={health['cache_hit_ratio']}")

    try:
        ln.finish()
    except Exception:
        pass
    print(f"\nexperiment_id = {experiment_id}\nnext: python analyze.py {experiment_id}")


if __name__ == "__main__":
    main()