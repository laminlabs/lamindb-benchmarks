"""Read benchmark parameters from a YAML config."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml

BENCHMARK_INSTANCE = "laminlabs/lamindb-benchmarks"
LOAD_INSTANCE = "laminlabs/lamindb-load-test"
BENCHMARK_INSTANCE_ENV = "LAMIN_CURRENT_INSTANCE"
LOAD_INSTANCE_ENV = "LAMIN_LOAD_INSTANCE"
CONFIG_ENV = "BENCHMARK_CONFIG"


def run_name(params: dict, *, default: str) -> str:
    """A display name; execution identity always remains the Lamin run uid."""
    value = params.get("name", default)
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 150:
        sys.exit("name must be a non-empty string of at most 150 characters")
    return value.strip()


def read_config(path: Path | str | None = None) -> dict:
    """Load a config file.

    An explicit path wins. Otherwise use BENCHMARK_CONFIG, then ./config.yml.
    Relative paths are resolved from the working directory.
    """
    if path is None:
        env_path = os.environ.get(CONFIG_ENV, "").strip()
        path = Path(env_path) if env_path else Path.cwd() / "config.yml"
    else:
        path = Path(path)
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()
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
