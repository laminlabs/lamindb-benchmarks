from __future__ import annotations
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter
from matplotlib.lines import Line2D
import pandas as pd

import config

DIST_SHEET = "DataDistributionAcrossDB"


def _sheet_df(type_name):
    import lamindb as ln
    t = ln.Record.filter(name=type_name, is_type=True).one()
    return ln.Record.filter(type=t).to_dataframe(include="features")


def _num(df, col):
    if col in df.columns:
        return pd.to_numeric(df[col], errors="coerce")
    return pd.Series([float("nan")] * len(df), index=df.index)


def _one_experiment(df, name):
    if "experiment_id" in df.columns:
        ids = df["experiment_id"].dropna().unique().tolist()
        if len(ids) > 1:
            raise SystemExit(
                f"{name} holds {len(ids)} experiments {ids} — TRACKING wasn't reset. "
                f"Reset tracking, or pass a specific experiment_id: python analyze.py <experiment_id>")
    return df


def _fmt(v, _=None):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return ""
    if v == 0:
        return "0"
    a = abs(v)
    if a >= 1e9:
        return f"{v/1e9:g}B"
    if a >= 1e6:
        return f"{v/1e6:g}M"
    if a >= 1e3:
        return f"{v/1e3:g}K"
    return f"{v:g}"

_FMT = FuncFormatter(_fmt)


def _axis(ax, xticks, logy=False):
    ax.set_xscale("log")
    xs = sorted({int(x) for x in xticks if x is not None and not pd.isna(x)})
    if xs:
        ax.set_xticks(xs)
    if logy:
        ax.set_yscale("log")
    ax.xaxis.set_major_formatter(_FMT)
    ax.yaxis.set_major_formatter(_FMT)
    ax.minorticks_off()


def _truthy(x):
    if x is None:
        return False
    if isinstance(x, str):
        return x.strip().lower() == "true"
    try:
        if pd.isna(x):
            return False
    except (TypeError, ValueError):
        pass
    return bool(x)


def _scan_kind(row):
    seq, idx = _truthy(row.get("used_seq_scan")), _truthy(row.get("used_index"))
    if idx and not seq:
        return "index"
    if seq and not idx:
        return "seq"
    if seq and idx:
        return "mixed"
    return "unknown"


def _render(snap, query, storage, experiment_id):
    snap = snap.copy()
    snap["scale_step"] = pd.to_numeric(snap["scale_step"], errors="coerce")
    snap = snap.sort_values("scale_step")
    steps = sorted(snap["scale_step"].dropna().unique().tolist())

    storage = storage.copy()
    if "scale_step" in storage.columns:
        storage["scale_step"] = pd.to_numeric(storage["scale_step"], errors="coerce")

    warm = query[query["cache_state"] == "warm"].copy() if "cache_state" in query.columns else query.copy()
    if "scale_step" in warm.columns:
        warm["scale_step"] = pd.to_numeric(warm["scale_step"], errors="coerce")

    fig, ax = plt.subplots(2, 4, figsize=(24, 11))
    wepr = _num(snap, "write_events_per_record").dropna()
    fig.suptitle(f"LaminDB indirect benchmark — {experiment_id}    "
                 f"(~{wepr.mean():.1f} write events per record)" if len(wepr) else experiment_id,
                 fontsize=14, fontweight="bold")

    a = ax[0, 0]
    a.plot(snap["scale_step"], _num(snap, "n_records"), "o-", label="records written")
    a.plot(snap["scale_step"], _num(snap, "n_events"), "s-", label="write events")
    a.set(xlabel="records written", ylabel="rows", title="Write events per record"); a.legend()
    _axis(a, steps, logy=True)

    a = ax[0, 1]
    aux = storage[storage["table_kind"].isin(["link", "data"])] if "table_kind" in storage.columns else storage.iloc[:0]
    for t, g in aux.groupby("table_name"):
        g = g.sort_values("scale_step")
        a.plot(g["scale_step"], _num(g, "n_rows"), "o-", label=t.replace("lamindb_", ""))
    a.set(xlabel="records written", ylabel="entries", title="Entries in auxiliary tables"); a.legend(fontsize=8)
    _axis(a, steps)

    a = ax[0, 2]
    a.plot(snap["scale_step"], _num(snap, "disk_space_bytes") / 1e6, "o-", color="tab:purple")
    a.set(xlabel="records written", ylabel="MB", title="Disk space occupied")
    _axis(a, steps)

    a = ax[0, 3]
    a.plot(snap["scale_step"], _num(snap, "insert_records_per_sec"), "o-", color="tab:red")
    a.set(xlabel="records written", ylabel="records / sec", title="Insert throughput")
    _axis(a, steps)

    a = ax[1, 0]
    if "query_type" in warm.columns:
        for qt, g in warm.groupby("query_type"):
            g = g.sort_values("scale_step")
            med = _num(g, "latency_median_ms")
            if not med.notna().any():
                continue
            line, = a.plot(g["scale_step"], med, "o-", label=f"{qt.replace('uid_','')} median")
            p95 = _num(g, "latency_p95_ms")
            if p95.notna().any():
                a.plot(g["scale_step"], p95, "o--", color=line.get_color(), alpha=0.7,
                       label=f"{qt.replace('uid_','')} p95")
    a.set(xlabel="records written", ylabel="ms", title="Client latency — median & p95 (incl. network)"); a.legend(fontsize=8)
    _axis(a, steps)

    a = ax[1, 1]
    if "query_type" in warm.columns and "execution_time_ms" in warm.columns:
        for qt, g in warm.dropna(subset=["execution_time_ms"]).groupby("query_type"):
            g = g.sort_values("scale_step")
            a.plot(g["scale_step"], _num(g, "execution_time_ms"), "o-", label=qt.replace("uid_", ""))
    a.set(xlabel="records written", ylabel="ms", title="DB execution time (network excluded)"); a.legend(fontsize=8)
    _axis(a, steps)

    a = ax[1, 2]
    COL = {"index": "tab:green", "seq": "tab:red", "mixed": "tab:orange", "unknown": "0.7"}
    MRK = {"index": "o", "seq": "X", "mixed": "s", "unknown": "."}
    if "query_type" in warm.columns and "used_seq_scan" in warm.columns:
        qtypes = list(dict.fromkeys(warm["query_type"]))
        ypos = {qt: i for i, qt in enumerate(qtypes)}
        for qt in qtypes:
            g = warm[warm["query_type"] == qt].sort_values("scale_step")
            a.plot(g["scale_step"], [ypos[qt]] * len(g), color="0.9", zorder=1)
        for _, r in warm.iterrows():
            k = _scan_kind(r)
            a.scatter(r["scale_step"], ypos[r["query_type"]], c=COL[k], marker=MRK[k], s=80, zorder=3)
        a.set_yticks(range(len(qtypes)))
        a.set_yticklabels([q.replace("uid_", "") for q in qtypes], fontsize=8)
        a.set_ylim(-0.5, len(qtypes) - 0.5)
        handles = [Line2D([0], [0], marker=MRK[k], color="w", markerfacecolor=COL[k],
                          markeredgecolor=COL[k], markersize=9, label=k)
                   for k in ("index", "seq", "mixed", "unknown")]
        a.legend(handles=handles, fontsize=7, loc="best")
    a.set(xlabel="records written", title="Query plan: scan type (green=index, red=seq)")
    a.set_xscale("log")
    if steps:
        a.set_xticks(steps)
    a.xaxis.set_major_formatter(_FMT)
    a.minorticks_off()

    a = ax[1, 3]
    a.plot(snap["scale_step"], _num(snap, "cache_hit_ratio"), "o-", color="tab:green")
    a.set(xlabel="records written", ylabel="ratio", title="Cache hit ratio", ylim=(0, 1.02))
    _axis(a, steps)

    fig.tight_layout(rect=[0, 0, 1, 0.96])
    return fig


def main(experiment_id=None):
    import lamindb as ln
    ln.connect(config.TRACKING_INSTANCE)
    ln.track()
    try:
        snap, query, storage = (_sheet_df("RunSnapshot"),
                                _sheet_df("QueryMeasurement"), _sheet_df(DIST_SHEET))

        if experiment_id is not None:
            available = set(snap["experiment_id"].dropna()) if "experiment_id" in snap.columns else set()
            if available and experiment_id not in available:
                raise SystemExit(f"experiment_id '{experiment_id}' not found. available: {sorted(available)}")
            snap, query, storage = (
                d[d["experiment_id"] == experiment_id] if "experiment_id" in d.columns else d
                for d in (snap, query, storage))
        else:
            snap = _one_experiment(snap, "RunSnapshot")
            query = _one_experiment(query, "QueryMeasurement")
            storage = _one_experiment(storage, DIST_SHEET)
            experiment_id = (snap["experiment_id"].iloc[0]
                             if "experiment_id" in snap.columns and len(snap) else "unknown")

        fig = _render(snap, query, storage, experiment_id)
        out = "benchmark_dashboard.svg"
        fig.savefig(out, format="svg")

        art = ln.Artifact(out, key="benchmarks/dashboard.svg").save()
        art.features.set_values({"experiment_id": experiment_id, "rls_on": bool(snap["rls_on"].iloc[0])})
        print(f"saved dashboard artifact {art.uid}")

        print(f"\nexperiment {experiment_id}")
        cols = [c for c in ("scale_step", "n_records", "n_events", "write_events_per_record") if c in snap.columns]
        view = snap.copy()
        view["scale_step"] = pd.to_numeric(view["scale_step"], errors="coerce")
        print(view.sort_values("scale_step")[cols].to_string(index=False))
    finally:
        ln.finish()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)