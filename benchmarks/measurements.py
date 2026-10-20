"""Write and read measurement parquet files. No database connection."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd

RESULTS_DIR_ENV = "BENCHMARK_RESULTS_DIR"
MANIFEST_NAME = "measurements.json"
COLUMNS = ("repetition", "latency_ms", "value")


def results_dir() -> Path | None:
    raw = os.environ.get(RESULTS_DIR_ENV)
    if not raw:
        return None
    path = Path(raw)
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_measurement(
    name: str,
    rows: list[dict],
    *,
    key: str,
    description: str,
    features: dict,
    columns: tuple[str, ...] = COLUMNS,
) -> Path | None:
    """Write one parquet file and record it in the results manifest.

    Returns None when the parent did not provide a results directory.
    """
    directory = results_dir()
    if directory is None:
        return None
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError(f"measurement name must be a file stem, got {name!r}")
    if not key.endswith(".parquet") or key.startswith("/") or ".." in Path(key).parts:
        raise ValueError(f"measurement key must be a relative parquet key, got {key!r}")

    file_name = f"{name}.parquet"
    frame = pd.DataFrame(rows, columns=list(columns))
    frame.to_parquet(directory / file_name, index=False)

    manifest_path = directory / MANIFEST_NAME
    entries = json.loads(manifest_path.read_text()) if manifest_path.is_file() else []
    entry = {
        "file": file_name,
        "key": key,
        "description": description,
        "features": features,
    }
    entries = [item for item in entries if item.get("file") != file_name]
    entries.append(entry)
    manifest_path.write_text(json.dumps(entries, indent=2) + "\n")
    return directory / file_name


def load_measurements(directory: Path) -> list[dict]:
    manifest_path = directory / MANIFEST_NAME
    if not manifest_path.is_file():
        return []
    entries = json.loads(manifest_path.read_text())
    if not isinstance(entries, list):
        raise ValueError(f"{manifest_path} must be a list")
    loaded = []
    root = directory.resolve()
    for entry in entries:
        file_name = entry["file"]
        if Path(file_name).name != file_name:
            raise ValueError(f"measurement file must be a basename, got {file_name!r}")
        path = (directory / file_name).resolve()
        if path.parent != root or not path.is_file():
            raise ValueError(f"measurement file not found: {file_name}")
        loaded.append({**entry, "path": path})
    return loaded
