"""Shared helpers for the lakehouse user-journey notebooks.

Each notebook imports from here so the timing helper, the analysis logic, and the
results schema are identical across engines. The notebooks themselves wrap each
step in an @ln.step() function for native LaminDB lineage; this module stays
framework-agnostic.

Design (see userjourney_benchmark.md):
  - S3-native: every engine reads/writes S3, never a local cache.
  - Developer-experience benchmark: LaminDB / Iceberg / LanceDB analyze in pandas
    (what their users actually do); DuckDB uses SQL.
  - QUERY_SOURCE = "store" (default): analytical queries read each engine's store.
    "memory": load once and compute in pandas. Set it in each notebook's config cell.
  - Ingest is timed separately from per-query work (the `category` field).
"""

import time
from contextlib import contextmanager

import pyarrow as pa
import pyarrow.compute as pc
import pandas as pd
import lamindb as ln

# canonical step -> category (drives the plots)
CATEGORY = {
    "read_from_lamin": "setup",
    "ingest": "setup",
    "query_stats": "query",
    "query_recurrent": "query",
    "filtered_query": "query",
    "append": "write",
    "schema_change": "write",
    "time_travel": "write",
}

# DuckDB-dialect SQL — used only by the DuckDB notebook (its native interface)
STATS_SQL = """
SELECT chrom                                    AS Chrom,
       COUNT(*)                                 AS Total_Variants,
       COUNT(*) FILTER (WHERE variant_type = 'DEL') AS Deletions,
       COUNT(*) FILTER (WHERE variant_type = 'INS') AS Insertions,
       AVG(af)                                  AS Mean_AF,
       AVG(eur_af)                              AS Mean_EUR_AF
FROM {src}
GROUP BY chrom
ORDER BY chrom
"""

RECURRENT_SQL = """
SELECT chrom || ':' || CAST((pos // 1000000) * 1000000 AS VARCHAR) AS region_key,
       COUNT(*)                                                      AS variant_count,
       AVG(af)                                                       AS mean_af
FROM {src}
GROUP BY region_key
HAVING COUNT(*) >= 2
ORDER BY variant_count DESC
"""


# --- shared pandas analysis (LaminDB / Iceberg / LanceDB) ---
def stats_pandas(df):
    """Per-chromosome variant summary statistics."""
    return (
        df.groupby("chrom")
        .agg(
            Total_Variants=("pos", "count"),
            Deletions=("variant_type", lambda x: (x == "DEL").sum()),
            Insertions=("variant_type", lambda x: (x == "INS").sum()),
            Mean_AF=("af", "mean"),
            Mean_EUR_AF=("eur_af", "mean"),
        )
        .reset_index()
        .rename(columns={"chrom": "Chrom"})
        .sort_values("Chrom")
    )


def recurrent_pandas(df, proximity=1_000_000):
    """Genomic regions with multiple variants (recurrent mutation hotspots)."""
    d = df[["chrom", "pos"]].copy()
    d["bin_start"] = (d["pos"] // proximity) * proximity
    d["region_key"] = d["chrom"].astype(str) + ":" + d["bin_start"].astype(str)
    counts = d.groupby("region_key")["pos"].count()
    return counts[counts >= 2]


class Bench:
    """Times named steps and records them in the canonical schema."""

    def __init__(self, engine):
        self.engine = engine
        self.timings = {}

    @contextmanager
    def __call__(self, step):
        t0 = time.perf_counter()
        yield
        dt = time.perf_counter() - t0
        self.timings[step] = dt
        print(f"  {step}: {dt:.3f}s")

    def record(self, query_source="store", n_runs=1):
        df = pd.DataFrame([
            {
                "engine": self.engine,
                "step": step,
                "category": CATEGORY.get(step, "other"),
                "seconds_median": secs,
                "seconds_std": 0.0,
                "n_runs": n_runs,
                "query_source": query_source,
            }
            for step, secs in self.timings.items()
        ])
        ln.Artifact.from_dataframe(
            df,
            key=f"benchmark_results/{self.engine}.parquet",
            description=f"{self.engine} pipeline timings ({query_source})",
        ).save()
        print(f"\nrecorded {len(df)} steps -> benchmark_results/{self.engine}.parquet")
        return df


def filter_params(df):
    """Pick a chromosome that exists and a POS band that contains rows, derived from
    the data so the point query can never silently return 0."""
    chrom = df["chrom"].value_counts().index[0]
    pos = df.loc[df["chrom"] == chrom, "pos"]
    return chrom, int(pos.quantile(0.1)), int(pos.quantile(0.9))


def make_append_batch(full_table):
    """Clone one chromosome's worth of variants as a 'new batch'."""
    if hasattr(full_table, "read_all"):
        full_table = full_table.read_all()
    # pick the smallest chrom by row count for a fast, bounded batch
    chroms = full_table.column("chrom").to_pandas().value_counts()
    smallest_chrom = chroms.index[-1]
    return full_table.filter(
        pc.equal(full_table["chrom"], pa.scalar(smallest_chrom))
    ).cast(full_table.schema)