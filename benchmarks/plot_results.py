"""Turn results/*.csv into labelled PNG charts (requires matplotlib: pip install matplotlib)."""
import csv, os, sys
import matplotlib  # type: ignore
matplotlib.use("Agg")
import matplotlib.pyplot as plt # type: ignore

res = sys.argv[1] if len(sys.argv) > 1 else "results"

def load(name):
    with open(os.path.join(res, name)) as f:
        return list(csv.DictReader(f))

def grouped_bar(rows, group, series, value, title, ylabel, fname, err=None):
    groups = list(dict.fromkeys(r[group] for r in rows))
    sers = list(dict.fromkeys(r[series] for r in rows))
    w = 0.8 / len(sers)
    fig, ax = plt.subplots(figsize=(8, 4.5))
    for i, s in enumerate(sers):
        vals = [float(next(r[value] for r in rows if r[group] == g and r[series] == s)) for g in groups]
        errs = [float(next(r[err] for r in rows if r[group] == g and r[series] == s)) for g in groups] if err else None
        ax.bar([x + i * w for x in range(len(groups))], vals, w, label=s, yerr=errs, capsize=3)
    ax.set_xticks([x + w * (len(sers) - 1) / 2 for x in range(len(groups))])
    ax.set_xticklabels(groups, fontsize=8)
    ax.set_title(title); ax.set_ylabel(ylabel); ax.legend(); fig.tight_layout()
    fig.savefig(os.path.join(res, fname), dpi=150); plt.close(fig)
    print("wrote", fname)

if os.path.exists(os.path.join(res, "m1_layout.csv")):
    grouped_bar(load("m1_layout.csv"), "workload", "layout", "ms_mean", "M1: row vs column layout (cold pool)", "time (ms, mean ± std)", "m1_layout.png", "ms_std")
if os.path.exists(os.path.join(res, "m1_policies.csv")):
    grouped_bar(load("m1_policies.csv"), "workload", "policy", "hit_ratio", "M1: buffer-pool hit ratio by replacement policy", "hit ratio", "m1_policies.png")
if os.path.exists(os.path.join(res, "m2_mixed.csv")):
    grouped_bar(load("m2_mixed.csv"), "workload", "index", "ops_per_s", "M2: B+ Tree vs LSM throughput", "operations / second", "m2_mixed.png", "std")
if os.path.exists(os.path.join(res, "m2_scan_vs_index.csv")):
    grouped_bar(load("m2_scan_vs_index.csv"), "query", "access_path", "ms_per_query", "M2: indexed lookup vs full scan", "ms per query (log scale)", "m2_scan_vs_index.png", "std")
