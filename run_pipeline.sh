#!/bin/bash
set -e

NOTEBOOKS=(
    "polars_pipeline.ipynb"
    "duckdb_pipeline.ipynb"
    "pyarrow_pipeline.ipynb"
    "iceberg_pipeline.ipynb"
    "lancedb_pipeline.ipynb"
)

for nb in "${NOTEBOOKS[@]}"; do
    echo "=== [$nb] started at $(date) ==="

    # clear all outputs + reset execution counts before running
    jupyter nbconvert \
        --to notebook \
        --ClearOutputsPreprocessor.enabled=True \
        --inplace \
        "$nb"

    jupyter nbconvert \
        --to notebook \
        --execute \
        --inplace \
        --ExecutePreprocessor.timeout=216000 \
        "$nb" \
        && echo "=== [$nb] DONE at $(date) ===" \
        || echo "=== [$nb] FAILED at $(date) ==="
done

echo "=== plotting results at $(date) ==="
python plot_benchmark.py
echo "All pipelines finished."
