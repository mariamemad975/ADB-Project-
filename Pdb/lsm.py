"""Log-Structured Merge Tree index (write-optimised)."""
"""LSM Tree, Writes go to a log file and an in-memory sorted table. When that table fills, it is saved as a sorted file on disk, and these files are merged (compaction) when there are too many. It recovers after a crash by replaying the log."""

import heapq
import json
import os
import struct
from bisect import bisect_left, bisect_right, insort
from .disk import DiskManager
from .keycodec import KeyCodec, MIN_I, MAX_I

PAGE_HDR = 4

class SSTable:
    def __init__(self, pool, path, kc, codec):
        self.pool, self.kc, self.path = pool, kc, path
        self.disk = DiskManager(path, codec)
        self.fid = pool.register_file(self.disk)
        self.ew = kc.width + 9
        self.cap = (self.disk.page_size - PAGE_HDR) // self.ew
        self.fences = []
        for pid in range(self.disk.num_pages):          # read heads directly, bypassing the pool
            buf = self.disk.read_page(pid)
            self.fences.append(self._entry(buf, 0)[0])
            if pid == self.disk.num_pages - 1:
                n = struct.unpack_from("<I", buf, 0)[0]
                self.max_entry = self._entry(buf, n - 1)[0]
        self.min_entry = self.fences[0]

    def _entry(self, buf, i):
        off = PAGE_HDR + i * self.ew
        k = self.kc.unpack(buf, off)
        p, s, a = struct.unpack_from("<iiB", buf, off + self.kc.width)
        return (k, p, s), bool(a)

    @staticmethod
    def write(path, items, kc, codec):
        """items: sorted iterable of ((key,p,s), alive). Returns number written (0 -> no file)."""
        ew = kc.width + 9
        disk = None
        cap = (4096 - PAGE_HDR) // ew
        buf, n, total = bytearray(4096), 0, 0

        def flush_page():
            nonlocal buf, n, disk
            if disk is None:
                disk = DiskManager(path, codec)
            struct.pack_into("<I", buf, 0, n)
            disk.write_page(disk.allocate_page(), buf)
            buf, n = bytearray(4096), 0
        for (k, p, s), alive in items:
            off = PAGE_HDR + n * ew
            buf[off:off + kc.width] = kc.pack(k)
            struct.pack_into("<iiB", buf, off + kc.width, p, s, 1 if alive else 0)
            n += 1
            total += 1
            if n == cap:
                flush_page()
        if n:
            flush_page()
        if disk:
            disk.close()
        return total

    def _page(self, pid):
        with self.pool.page(self.fid, pid) as fr:
            n = struct.unpack_from("<I", fr.data, 0)[0]
            return [self._entry(fr.data, i) for i in range(n)]

    def iter_range(self, lo_e=None, hi_e=None):
        if (hi_e is not None and hi_e < self.min_entry) or (lo_e is not None and lo_e > self.max_entry):
            return
        pid = 0 if lo_e is None else max(0, bisect_right(self.fences, lo_e) - 1)
        while pid < self.disk.num_pages:
            for e, alive in self._page(pid):
                if lo_e is not None and e < lo_e:
                    continue
                if hi_e is not None and e > hi_e:
                    return
                yield e, alive
            pid += 1

    def close(self, delete=False):
        self.pool.drop_file(self.fid)
        if delete:
            os.remove(self.path)


class LSMTree:
    kind = "lsm"

    def __init__(self, path, ktype, pool, codec=None, memtable_limit=4096, max_tables=4, sync_wal=False):
        self.dir, self.pool, self.codec = path, pool, codec
        self.kc = KeyCodec(ktype)
        self.memtable_limit, self.max_tables, self.sync_wal = memtable_limit, max_tables, sync_wal
        os.makedirs(path, exist_ok=True)
        self.manifest_path = os.path.join(path, "manifest.json")
        self.wal_path = os.path.join(path, "wal.log")
        self.mem, self.mem_sorted = {}, []
        self.flushes = self.compactions = 0
        if os.path.exists(self.manifest_path):
            with open(self.manifest_path) as f:
                m = json.load(f)
        else:
            m = {"next_id": 0, "tables": []}
        self.next_id = m["next_id"]
        self.tables = [SSTable(self.pool, self._sst_path(i), self.kc, codec) for i in m["tables"]]
        self.table_ids = list(m["tables"])
        self._replay_wal()
        self.wal = open(self.wal_path, "a", buffering=1)

    # bookkeeping
    def _sst_path(self, i):
        return os.path.join(self.dir, f"sst_{i:06d}.sst")

    def _save_manifest(self):
        tmp = self.manifest_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"next_id": self.next_id, "tables": self.table_ids}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.manifest_path)

    def _replay_wal(self):
        if not os.path.exists(self.wal_path):
            return
        with open(self.wal_path) as f:
            for line in f:
                try:
                    op, k, p, s = json.loads(line)
                except ValueError:
                    break                                  # torn last record
                self._mem_put((k, p, s), op == "i")

    def _mem_put(self, e, alive):
        if e not in self.mem:
            insort(self.mem_sorted, e)
        self.mem[e] = alive

    def _log(self, op, e):
        self.wal.write(json.dumps([op, e[0], e[1], e[2]]) + "\n")
        if self.sync_wal:
            self.wal.flush()
            os.fsync(self.wal.fileno())

    # write
    def insert(self, key, rid):
        self.kc.check(key)
        self.kc.pack(key)                           # validate width early
        e = (key, rid[0], rid[1])
        self._log("i", e)
        self._mem_put(e, True)
        self._maybe_flush()

    def delete(self, key, rid):
        e = (key, rid[0], rid[1])
        self._log("d", e)
        self._mem_put(e, False)
        self._maybe_flush()
        return True

    def _maybe_flush(self):
        if len(self.mem) >= self.memtable_limit:
            self.flush_memtable()

    def flush_memtable(self):
        if not self.mem:
            return
        sid = self.next_id
        self.next_id += 1
        path = self._sst_path(sid)
        SSTable.write(path, ((e, self.mem[e]) for e in self.mem_sorted), self.kc, self.codec)
        self.tables.append(SSTable(self.pool, path, self.kc, self.codec))
        self.table_ids.append(sid)
        self._save_manifest()
        self.mem, self.mem_sorted = {}, []
        self.wal.close()
        self.wal = open(self.wal_path, "w", buffering=1)    # truncate: memtable is durable now
        self.flushes += 1
        if len(self.tables) > self.max_tables:
            self.compact()

    def compact(self):
        """Merge every SSTable into one; newest entry wins, tombstones are dropped."""
        if len(self.tables) < 2:
            return

        def stream(t, prio):
            for e, alive in t.iter_range():
                yield e, alive, prio

        merged = heapq.merge(*[stream(t, i) for i, t in enumerate(self.tables)],
                             key=lambda x: (x[0], -x[2]))

        def live_items():
            last = None
            for e, alive, _ in merged:
                if e == last:
                    continue
                last = e
                if alive:
                    yield e, True

        sid = self.next_id
        self.next_id += 1
        path = self._sst_path(sid)
        n = SSTable.write(path, live_items(), self.kc, self.codec)
        old, old_ids = self.tables, self.table_ids
        self.tables = [SSTable(self.pool, path, self.kc, self.codec)] if n else []
        self.table_ids = [sid] if n else []
        self._save_manifest()
        for t in old:
            t.close(delete=True)
        self.compactions += 1

    # read
    def range(self, lo=None, hi=None):
        lo_e = None if lo is None else (lo, MIN_I, MIN_I)
        hi_e = None if hi is None else (hi, MAX_I, MAX_I)
        state = {}
        for t in self.tables:                              # oldest -> newest
            for e, alive in t.iter_range(lo_e, hi_e):
                state[e] = alive
        a = 0 if lo_e is None else bisect_left(self.mem_sorted, lo_e)
        b = len(self.mem_sorted) if hi_e is None else bisect_right(self.mem_sorted, hi_e)
        for e in self.mem_sorted[a:b]:
            state[e] = self.mem[e]
        for e in sorted(e for e, alive in state.items() if alive):
            yield e[0], (e[1], e[2])

    def search(self, key):
        return [rid for _, rid in self.range(key, key)]

    # misc
    def size_bytes(self):
        total = sum(t.disk.size_bytes() for t in self.tables)
        return total + (os.path.getsize(self.wal_path) if os.path.exists(self.wal_path) else 0)

    @property
    def num_tables(self):
        return len(self.tables)

    def flush(self):
        self.wal.flush()
        self.pool.flush_all()

    def close(self):
        self.flush_memtable()
        self.wal.close()
        for t in self.tables:
            t.close()
        self.tables = []

    def _simulate_crash(self):
        """Test helper: drop in-memory state without flushing."""
        self.wal.flush()
        self.wal.close()
        for t in self.tables:
            t.close()
        self.tables = []
