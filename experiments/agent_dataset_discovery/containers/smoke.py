"""Executed inside each built image, with networking disabled."""

import importlib.metadata
import importlib.util
import os
import subprocess
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import scanpy as sc

assert os.getuid() != 0
assert Path.cwd() == Path("/workspace")
assert not list(Path.cwd().iterdir())
assert not list(Path(os.environ["CODEX_HOME"]).iterdir())
assert duckdb.sql("select 42").fetchall() == [(42,)]
assert pa.Table.from_pandas(pd.DataFrame({"x": [1]})).num_rows == 1
assert sc.AnnData(pd.DataFrame([[0, 1]])).shape == (1, 2)

backend = os.environ["DISCOVERY_BACKEND"]
if backend == "s3":
    assert importlib.util.find_spec("boto3") is not None
    assert importlib.util.find_spec("lamindb") is None
    subprocess.run(["aws", "--version"], check=True)
elif backend == "lamin":
    # Importing Lamin itself can require an instance; inspect the installation offline.
    assert importlib.metadata.version("lamindb") == "2.12.0"
    assert importlib.util.find_spec("lamindb_setup") is not None
else:
    raise AssertionError(f"Unknown backend: {backend}")
subprocess.run(["python", "-m", "pip", "check"], check=True)
subprocess.run(["codex", "--version"], check=True)
print(f"Smoke checks passed: codex / {backend}")
