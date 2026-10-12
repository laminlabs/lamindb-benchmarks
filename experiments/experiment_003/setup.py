"""Create Samples records with one date stored in RecordJson.

Uses bulk_create. Dates are shuffled, and about 10% of records have no
collected_on row, so ordering the sheet is a left join with nulls. Does not
call ln.track(): the load instance cannot finish while its Storage row is missing.

Usage:
  python experiments/experiment_003/setup.py
"""
from __future__ import annotations

import random
import sys
import time
from datetime import date, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import lamindb as ln

from benchmarks.config import (
    LOAD_INSTANCE,
    LOAD_INSTANCE_ENV,
    instance_from_env,
    read_config,
    require_int,
)
from benchmarks.measurements import write_measurement

NAME = Path(__file__).resolve().parent.name
TYPE_NAME = "Samples"
FEATURE_NAME = "collected_on"
BATCH = 2000
SEED = 2
MISSING_FRACTION = 0.10
DATE_START = date(2010, 1, 1)
DATE_END = date(2024, 12, 31)


def main() -> None:
    n_rows = require_int(read_config(), "n_rows", minimum=1)
    ln.connect(instance_from_env(LOAD_INSTANCE_ENV, LOAD_INSTANCE))
    # ln.track(params={"n_rows": n_rows})
    started = time.perf_counter()
    created, json_rows, missing = create_samples(n_rows)
    elapsed = time.perf_counter() - started
    _write_populate(n_rows, created, json_rows, missing, elapsed)
    print(
        f"setup created {created} samples, {json_rows} json rows, "
        f"{missing} missing dates in {elapsed:.1f}s"
    )
    # ln.finish()


def create_samples(n_rows: int) -> tuple[int, int, int]:
    samples, feature = ensure_sheet()
    _clear_samples(samples)
    rng = random.Random(SEED)
    missing_at = set(rng.sample(range(n_rows), round(n_rows * MISSING_FRACTION)))
    dates = _shuffled_dates(rng, n_rows)
    records = _insert_records(samples, n_rows)
    json_rows = _insert_json(records, feature, dates, missing_at)
    return len(records), json_rows, len(missing_at)


def ensure_sheet() -> tuple[ln.Record, ln.Feature]:
    samples = ensure_type(TYPE_NAME)
    feature = ensure_feature(FEATURE_NAME, date)
    if samples.schema is None:
        samples.schema = ln.Schema(
            [feature],
            name="samples_schema",
            minimal_set=True,
        ).save()
        samples.save()
    return samples, feature


def _clear_samples(samples: ln.Record) -> None:
    """A lone rerun must not append. The batch wipe also empties the instance."""
    ln.Record.filter(type=samples, is_type=False).delete()


def _insert_records(samples: ln.Record, n_rows: int) -> list[ln.Record]:
    created: list[ln.Record] = []
    for start in range(0, n_rows, BATCH):
        count = min(BATCH, n_rows - start)
        rows = [
            ln.Record(
                name=f"{NAME}_{start + offset:06d}",
                type=samples,
                space_id=samples.space_id,
                branch_id=samples.branch_id,
            )
            for offset in range(count)
        ]
        created.extend(ln.Record.objects.bulk_create(rows, batch_size=BATCH))
    if len(created) != n_rows or any(record.id is None for record in created):
        sys.exit(f"bulk_create returned {len(created)} records, expected {n_rows}")
    return created


def _insert_json(
    records: list[ln.Record],
    feature: ln.Feature,
    dates: list[date],
    missing_at: set[int],
) -> int:
    from lamindb.models import RecordJson

    rows = [
        RecordJson(
            record_id=record.id,
            feature_id=feature.id,
            value=dates[index].isoformat(),
        )
        for index, record in enumerate(records)
        if index not in missing_at
    ]
    for start in range(0, len(rows), BATCH):
        RecordJson.objects.bulk_create(rows[start : start + BATCH], batch_size=BATCH)
    return len(rows)


def _shuffled_dates(rng: random.Random, n_rows: int) -> list[date]:
    span = (DATE_END - DATE_START).days
    dates = [
        DATE_START + timedelta(days=rng.randrange(span + 1)) for _ in range(n_rows)
    ]
    rng.shuffle(dates)
    return dates


def _write_populate(
    n_rows: int,
    created: int,
    json_rows: int,
    missing: int,
    elapsed: float,
) -> None:
    written = write_measurement(
        "populate",
        [{"repetition": 1, "latency_ms": round(elapsed * 1000.0, 3), "value": created}],
        key=f"benchmarks/measurements/{NAME}/populate.parquet",
        description=(
            f"bulk_create of {created} Samples and {json_rows} collected_on "
            f"JSON rows, {missing} dates left missing"
        ),
        features={
            "measurement_kind": "insert",
            "query_name": "populate",
            "n_rows": n_rows,
            "n_records": created,
            "n_json_rows": json_rows,
            "n_missing_dates": missing,
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


if __name__ == "__main__":
    main()
