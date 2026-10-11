"""Clock a callable. No database connection and no tracking."""
from __future__ import annotations

import statistics
import time


def time_calls(fn, repetitions: int) -> dict:
    if repetitions < 1:
        raise ValueError("repetitions must be >= 1")
    latencies_ms = []
    values = []
    for _ in range(repetitions):
        started = time.perf_counter()
        values.append(fn())
        latencies_ms.append(round((time.perf_counter() - started) * 1000.0, 3))
    return {
        "n_repetitions": repetitions,
        "latencies_ms": latencies_ms,
        "latency_min_ms": min(latencies_ms),
        "latency_median_ms": statistics.median(latencies_ms),
        "latency_max_ms": max(latencies_ms),
        "values": values,
        "last_value": values[-1],
    }


def rows_from_timed(timed: dict) -> list[dict]:
    return [
        {"repetition": index, "latency_ms": latency, "value": value}
        for index, (latency, value) in enumerate(
            zip(timed["latencies_ms"], timed["values"]), start=1
        )
    ]
