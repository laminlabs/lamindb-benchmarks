"""Create plain records for a load-instance experiment."""
from __future__ import annotations

from datetime import datetime, timezone

import lamindb as ln


def create_records(n_rows: int, *, name_prefix: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    records = [
        ln.Record(
            name=f"{name_prefix}_{stamp}_{i:05d}",
            description="example benchmark record",
            reference=str(i),
            reference_type="row_index",
        )
        for i in range(n_rows)
    ]
    if records:
        ln.save(records)
