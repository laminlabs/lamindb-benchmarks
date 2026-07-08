"""Collect benchmark results from LaminDB and produce comparison plots.

Tracked with ln.track() so the lineage graph shows:
  benchmark_results/{engine}.parquet  (x4, inputs)
      -> plots.py
          -> plots/setup_cost.svg
          -> plots/query_times.svg
          -> plots/write_path.svg

Run: python plots.py
"""

import lamindb as ln
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
from pathlib import Path

ln.track("0BTaXwKDZVRM", project="Lakehouse benchmarks v1")

# ── 1. Load results from LaminDB ──────────────────────────────────────────
ENGINES = ["pyarrow", "polars", "duckdb", "iceberg", "lancedb"]

frames = []
for engine in ENGINES:
    art = ln.Artifact.get(key=f"benchmark_results/{engine}.parquet")
    df  = art.load()
    frames.append(df)
    print(f"loaded {engine}: {len(df)} steps")

results = pd.concat(frames, ignore_index=True)
print(f"\nTotal rows: {len(results)}")
print(results[["engine", "step", "seconds_median"]].to_string(index=False))

# ── 2. Pivot ───────────────────────────────────────────────────────────────
def secs(engine, step):
    row = results[(results["engine"] == engine) & (results["step"] == step)]
    return float(row["seconds_median"].iloc[0]) if len(row) else None

DISPLAY = {
    "pyarrow": "PyArrow",
    "polars":          "Polars",
    "duckdb":          "DuckDB",
    "iceberg":         "Iceberg",
    "lancedb":         "LanceDB",
}
COLORS = {
    "pyarrow": "#4C72B0",
    "polars":          "#8B5CF6",
    "duckdb":          "#DD8452",
    "iceberg":         "#55A868",
    "lancedb":         "#C44E52",
}

ENG_DISP = [DISPLAY[e] for e in ENGINES]

RAW = {
    DISPLAY[e]: {step: secs(e, step) for step in results["step"].unique()}
    for e in ENGINES
}

# ── 3. Helpers ────────────────────────────────────────────────────────────
OUT = Path("plots")
OUT.mkdir(exist_ok=True)
saved_paths = []

def save(fig, name):
    p = OUT / name
    fig.savefig(p, dpi=150, bbox_inches="tight")
    plt.close(fig)
    saved_paths.append(p)
    print(f"saved {p}")

def note(ax, txt, top=True):
    y, va = (0.98, "top") if top else (0.02, "bottom")
    ax.annotate(txt, xy=(0.01, y), xycoords="axes fraction",
                fontsize=7.5, color="#555", va=va,
                bbox=dict(boxstyle="round,pad=0.3", fc="#f9f9f9", ec="#ccc", alpha=0.9))

# ── 4. Chart 1 — Setup cost ───────────────────────────────────────────────
fig, ax = plt.subplots(figsize=(8, 5))
x     = np.arange(len(ENGINES))
width = 0.5

reads   = [RAW[DISPLAY[e]].get("read_from_lamin") or 0 for e in ENGINES]
ingests = [RAW[DISPLAY[e]].get("ingest")          or 0 for e in ENGINES]

ax.bar(x, reads, width,
       color=[COLORS[e] for e in ENGINES], alpha=0.9,
       label="Read from LaminDB (S3)")
ax.bar(x, ingests, width, bottom=reads,
       color=[COLORS[e] for e in ENGINES], alpha=0.4,
       hatch=["xx" if v > 0 else "" for v in ingests],
       label="Ingest into engine store")

for i, (r, ing) in enumerate(zip(reads, ingests)):
    total = r + ing
    ax.text(i, total + 0.15, f"{total:.2f}s",
            ha="center", va="bottom", fontsize=9, fontweight="bold")

ax.set_xticks(x)
ax.set_xticklabels(ENG_DISP, fontsize=11)
ax.set_ylabel("Time (seconds)", fontsize=10)
ax.set_title("Setup cost: read from LaminDB + ingest into engine store",
             fontsize=12, fontweight="bold", pad=12)
ax.set_ylim(0, max(r + i for r, i in zip(reads, ingests)) * 1.25)
ax.spines[["top", "right"]].set_visible(False)
ax.legend(handles=[
    mpatches.Patch(color="#888", alpha=0.9,  label="Read from LaminDB (S3)"),
    mpatches.Patch(color="#888", alpha=0.4, hatch="xx", label="Ingest into engine store"),
], fontsize=8, loc="upper left", framealpha=0.9)
plt.tight_layout()
save(fig, "setup_cost.svg")

# ── 5. Chart 2 — Query times ──────────────────────────────────────────────
QUERY_STEPS = ["query_stats", "query_recurrent", "filtered_query"]
STEP_LABELS = ["Per-sample\nstatistics", "Recurrent\nregions",
               "Filtered query\n(native pushdown)"]

EXCLUDE = set()   # all engines have valid query_recurrent timings

fig, ax = plt.subplots(figsize=(9, 5))
n = len(ENGINES)
w = 0.18
offsets = np.linspace(-(n-1)/2*w, (n-1)/2*w, n)
xpos    = np.arange(len(QUERY_STEPS))

for i, e in enumerate(ENGINES):
    vals, missing = [], []
    for step in QUERY_STEPS:
        v = RAW[DISPLAY[e]].get(step)
        if (e, step) in EXCLUDE:
            v = None
        vals.append(v if v is not None else 0)
        missing.append(v is None)
    bars = ax.bar(xpos + offsets[i], vals, w,
                  label=DISPLAY[e], color=COLORS[e], alpha=0.85)
    for b, m, v in zip(bars, missing, vals):
        h = b.get_height()
        if m:
            ax.text(b.get_x() + b.get_width()/2, 0.01, "N/A",
                    ha="center", va="bottom", fontsize=7, color="#aaa", rotation=90)
        elif h > 0:
            lbl = f"{h:.2f}s" if h >= 0.1 else f"{h:.3f}s"
            ax.text(b.get_x() + b.get_width()/2, h + 0.01, lbl,
                    ha="center", va="bottom", fontsize=7, rotation=45)

ax.set_xticks(xpos)
ax.set_xticklabels(STEP_LABELS, fontsize=10)
ax.set_ylabel("Time (seconds)", fontsize=10)
ax.set_title("Query times by engine — all on S3", fontsize=12, fontweight="bold", pad=12)
ax.spines[["top", "right"]].set_visible(False)
ax.legend(fontsize=9, loc="upper right", framealpha=0.9)
plt.tight_layout()
save(fig, "query_times.svg")

# ── 6. Chart 3 — Write-path times ────────────────────────────────────────
WRITE_STEPS  = ["append", "schema_change", "time_travel"]
WRITE_LABELS = ["Append\nnew sample", "Schema\nchange", "Time travel"]

fig, ax = plt.subplots(figsize=(9, 5))
xpos = np.arange(len(WRITE_STEPS))

for i, e in enumerate(ENGINES):
    vals    = [RAW[DISPLAY[e]].get(step) or 0 for step in WRITE_STEPS]
    missing = [RAW[DISPLAY[e]].get(step) is None for step in WRITE_STEPS]
    bars = ax.bar(xpos + offsets[i], vals, w,
                  label=DISPLAY[e], color=COLORS[e], alpha=0.85)
    for b, m, v in zip(bars, missing, vals):
        h = b.get_height()
        if m:
            ax.text(b.get_x() + b.get_width()/2, 0.02, "N/A",
                    ha="center", va="bottom", fontsize=7, color="#aaa", rotation=90)
        elif h > 0:
            lbl = f"{h:.2f}s" if h >= 0.1 else f"{h:.3f}s"
            ax.text(b.get_x() + b.get_width()/2, h + 0.02, lbl,
                    ha="center", va="bottom", fontsize=7, rotation=45)

ax.set_xticks(xpos)
ax.set_xticklabels(WRITE_LABELS, fontsize=10)
ax.set_ylabel("Time (seconds)", fontsize=10)
ax.set_title("Write-path times by engine — all on S3", fontsize=12, fontweight="bold", pad=12)
ax.spines[["top", "right"]].set_visible(False)
ax.legend(fontsize=9, loc="upper right", framealpha=0.9)
plt.tight_layout()
save(fig, "write_path.svg")

# ── 8. Save plots to LaminDB ─────────────────────────────────────────────
for p in saved_paths:
    art = ln.Artifact(str(p), key=f"plots/{p.name}",
                      description=f"Benchmark plot: {p.stem}")
    art.save()
    print(f"registered {p.name} -> {art.uid}")

ln.finish()
print("\nDone. Lineage: 4 benchmark parquets -> plots.py -> 3 SVG artifacts")