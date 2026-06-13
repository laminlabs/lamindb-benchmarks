# lakehouse-benchmarks

Benchmarking DuckDB, Iceberg, and LaminDB as Lakehouse query engines over Parquet.

## Install

```bash
pip install -e ".[all]"
```

## Run

```bash
python scripts/run_loading_benchmark_on_collection.py duckdb
python scripts/run_loading_benchmark_on_collection.py iceberg
python scripts/run_loading_benchmark_on_collection.py lamindb
```

## Plot

```bash
python scripts/plot_benchmarks.py
```