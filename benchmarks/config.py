"""Read benchmark parameters from ./config.yml."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml

BENCHMARK_INSTANCE = "laminlabs/lamindb-benchmarks"
LOAD_INSTANCE = "laminlabs/lamindb-load-test"
BENCHMARK_INSTANCE_ENV = "LAMIN_CURRENT_INSTANCE"
LOAD_INSTANCE_ENV = "LAMIN_LOAD_INSTANCE"


def read_config() -> dict:
    path = (Path.cwd() / "config.yml").resolve()
    if not path.is_file():
        sys.exit(f"config not found: {path}")
    loaded = yaml.safe_load(path.read_text())
    if not isinstance(loaded, dict):
        sys.exit("config must be a YAML mapping")
    return loaded


def instance_from_env(env_name: str, default: str) -> str:
    """Instance slug from the environment, or the code default when unset."""
    value = os.environ.get(env_name, "").strip()
    return value or default


def require_str(params: dict, key: str, default: str | None = None) -> str:
    value = params.get(key, default)
    if not isinstance(value, str) or not value.strip():
        sys.exit(f"{key} must be a non-empty string")
    return value


def require_int(
    params: dict,
    key: str,
    *,
    minimum: int,
    default: int | None = None,
) -> int:
    if (key not in params or params[key] is None) and default is not None:
        return default
    value = params.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        sys.exit(f"{key} must be an integer >= {minimum}")
    return value
