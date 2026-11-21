"""B+ Tree vs LSM Tree, indexed lookup vs full scan, read/write-heavy mixes."""
"""The performance comparisons, and the index selection rule, are based on the workload profile (counts of point reads, range reads, writes) observed during the warm-up run. """
import argparse, os, random, shutil, tempfile, time
from common import *
from Pdb.sql import parse
from Pdb import Database, BPlusTree, LSMTree, BufferPool, WorkloadProfile, choose_index

FRAMES = 256
def new_index(kind, d):
    pool = BufferPool(FRAMES, "lru")
    idx = (BPlusTree(os.path.join(d, "i.bpt"), "INT", pool) if kind == "btree"
           else LSMTree(os.path.join(d, "i.lsm"), "INT", pool, memtable_limit=4096))
    return idx, pool

def exp_microbench(a):
    print("\n[1] B+ Tree vs LSM Tree micro-benchmarks (n=%d keys, random insert order)" % a.n)
    metrics = {}
    for kind in ("btree", "lsm"):
        runs = []
        for r in range(a.runs + 1):
            d = tempfile.mkdtemp()
            try:
                rng = random.Random(a.seed)
                keys = list(range(a.n)); rng.shuffle(keys)
                idx, pool = new_index(kind, d)
                s = time.perf_counter()
                for k in keys:
                    idx.insert(k, (k // 100, k % 100))
                t_ins = time.perf_counter() - s
                s = time.perf_counter(); idx.flush();
                if kind == "lsm": idx.flush_memtable()
                t_flush = time.perf_counter() - s
                pool.clear()
                probes = [rng.randrange(a.n) for _ in range(3000)]
                lat = []
                for k in probes:
                    t0 = time.perf_counter(); res = idx.search(k); lat.append((time.perf_counter() - t0) * 1e6)
                    assert res == [(k // 100, k % 100)]
                s = time.perf_counter()
                for _ in range(100):
                    lo = rng.randrange(a.n - 100)
                    assert len(list(idx.range(lo, lo + 99))) == 100
                t_rng = time.perf_counter() - s
                runs.append({"insert_s": t_ins, "flush_s": t_flush, "lookup_p50_us": percentile(lat, 50),
                             "lookup_p95_us": percentile(lat, 95), "range100_ms": t_rng * 10,
                             "size_kb": idx.size_bytes() / 1024, "ins_ops": a.n / t_ins})
                idx.close()
            finally:
                shutil.rmtree(d, ignore_errors=True)
        runs = runs[1:]                                        # drop warm-up
        row = {"index": kind, "n": a.n}
        for k in runs[0]:
            m, sd = mean_std([r[k] for r in runs])
            row[k] = round(m, 3); row[k + "_std"] = round(sd, 3)
        metrics[kind] = row
    rows = list(metrics.values())
    print_table(rows, ["index", "insert_s", "insert_s_std", "ins_ops", "flush_s", "lookup_p50_us", "lookup_p95_us", "range100_ms", "size_kb"])
    write_csv(os.path.join(a.out, "m2_micro.csv"), rows)

def exp_mixed(a):
    ops = 20000
    print("\n[2] Mixed workloads on %d preloaded keys, %d ops each (throughput, ops/s)" % (a.n // 2, ops))
    rows = []
    for name, write_frac in (("read-heavy 95/5", 0.05), ("write-heavy 5/95", 0.95)):
        for kind in ("btree", "lsm"):
            vals = []
            for r in range(a.runs + 1):
                d = tempfile.mkdtemp()
                try:
                    rng = random.Random(a.seed)
                    idx, pool = new_index(kind, d)
                    for k in range(a.n // 2):
                        idx.insert(k, (k, 0))
                    nxt, s = a.n // 2, None
                    plan = [rng.random() < write_frac for _ in range(ops)]
                    s = time.perf_counter()
                    for is_w in plan:
                        if is_w:
                            idx.insert(nxt, (nxt, 0)); nxt += 1
                        else:
                            idx.search(rng.randrange(a.n // 2))
                    vals.append(ops / (time.perf_counter() - s))
                    idx.close()
                finally:
                    shutil.rmtree(d, ignore_errors=True)
            m, sd = mean_std(vals[1:])
            rows.append({"workload": name, "index": kind, "ops_per_s": int(m), "std": int(sd)})
    print_table(rows, ["workload", "index", "ops_per_s", "std"])
    write_csv(os.path.join(a.out, "m2_mixed.csv"), rows)
    print("\n  Advisor decisions for these workloads:")
    for name, prof in (("read-heavy 95/5", WorkloadProfile(950, 0, 50)), ("write-heavy 5/95", WorkloadProfile(50, 0, 950)),
                       ("range-heavy", WorkloadProfile(100, 600, 300))):
        print(f"   {name:18s} -> {choose_index(prof)}")

def exp_scan_vs_index(a):
    print("\n[3] Indexed lookup vs full table scan (SQL layer, row layout, n=%d)" % a.n)
    rows = []
    d = tempfile.mkdtemp()
    try:
        db = Database(d, buffer_frames=FRAMES)
        db.execute("CREATE TABLE products (id INT, category TEXT(16), price FLOAT)")
        rng = random.Random(a.seed)
        db.table("products").insert_many([(i, rng.choice(["a", "b", "c", "d"]), rng.random() * 100) for i in range(a.n)])
        db.execute("CREATE INDEX ix_id ON products (id) USING btree")
        t = db.table("products")
        probes = [rng.randrange(a.n) for _ in range(200)]
        for label, sql_for in (("point lookup", lambda k: f"SELECT * FROM products WHERE id = {k}"),
                               ("range 1% of table", lambda k: f"SELECT * FROM products WHERE id BETWEEN {k} AND {k + a.n // 100}")):
            for path in ("full scan", "B+ Tree index"):
                times = []
                n_q = 20 if path == "full scan" else 200
                for r in range(a.runs + 1):
                    s = time.perf_counter()
                    for k in probes[:n_q]:
                        res = t.select(None, parse(sql_for(k))["where"], use_index=(path != "full scan"))
                    times.append((time.perf_counter() - s) / n_q * 1000)
                m, sd = mean_std(times[1:])
                rows.append({"query": label, "access_path": path, "ms_per_query": round(m, 3), "std": round(sd, 3)})
        print_table(rows, ["query", "access_path", "ms_per_query", "std"])
        for q in ("point lookup", "range 1% of table"):
            sc = next(r for r in rows if r["query"] == q and r["access_path"] == "full scan")["ms_per_query"]
            ix = next(r for r in rows if r["query"] == q and r["access_path"] == "B+ Tree index")["ms_per_query"]
            print(f"   speed-up for {q}: {sc / ix:.0f}x")
        write_csv(os.path.join(a.out, "m2_scan_vs_index.csv"), rows)
        db.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20000)
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default="results")
    a = ap.parse_args()
    header("M2 index benchmarks", a, f"buffer frames={FRAMES}")
    exp_microbench(a)
    exp_mixed(a)
    exp_scan_vs_index(a)
