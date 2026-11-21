import csv, os, platform, statistics, sys, time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

def machine_info():
    cpu = platform.processor() or platform.machine()
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    cpu = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    return {"python": sys.version.split()[0], "os": platform.platform(), "cpu": cpu, "cores": os.cpu_count()}

def percentile(vals, p):
    s = sorted(vals)
    if not s:
        return 0.0
    k = (len(s) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)

def mean_std(vals):
    return statistics.mean(vals), (statistics.stdev(vals) if len(vals) > 1 else 0.0)

def write_csv(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"  -> wrote {path}")

def print_table(rows, cols):
    widths = [max(len(c), *(len(str(r[c])) for r in rows)) for c in cols]
    print("  " + " | ".join(c.ljust(w) for c, w in zip(cols, widths)))
    print("  " + "-+-".join("-" * w for w in widths))
    for r in rows:
        print("  " + " | ".join(str(r[c]).ljust(w) for c, w in zip(cols, widths)))

def header(title, args, extra=None):
    print("=" * 78)
    print(title)
    print("machine:", machine_info())
    print("args:", vars(args), extra or "")
    print("=" * 78)
