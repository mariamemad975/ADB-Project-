"""Buffer pool with pluggable replacement policies: LRU, FIFO, CLOCK"""
from collections import OrderedDict
from contextlib import contextmanager

class BufferPoolFullError(RuntimeError):
    pass

class Frame:
    __slots__ = ("key", "data", "dirty", "pins")
    def __init__(self, key, data):
        self.key, self.data, self.dirty, self.pins = key, data, False, 0

# policies
class LRUPolicy:
    name = "lru"
    def __init__(self):
        self.od = OrderedDict()

    def on_insert(self, k): self.od[k] = None
    def on_access(self, k): self.od.move_to_end(k)
    def on_remove(self, k): self.od.pop(k, None)

    def victim(self, evictable):
        for k in self.od:
            if evictable(k):
                return k


class FIFOPolicy(LRUPolicy):
    name = "fifo"
    def on_access(self, k):  # hits do not change the order
        pass

class ClockPolicy:
    """Second-chance clock replacement policy"""
    name = "clock"

    def __init__(self):
        self.ring, self.ref, self.hand = [], {}, 0

    def on_insert(self, k):
        self.ring.append(k)
        self.ref[k] = True

    def on_access(self, k): self.ref[k] = True

    def on_remove(self, k):
        i = self.ring.index(k)
        self.ring.pop(i)
        del self.ref[k]
        if i < self.hand:
            self.hand -= 1
        if self.hand >= len(self.ring):
            self.hand = 0

    def victim(self, evictable):
        for _ in range(2 * len(self.ring) + 1):
            if not self.ring:
                return None
            if self.hand >= len(self.ring):
                self.hand = 0
            k = self.ring[self.hand]
            if evictable(k):
                if self.ref[k]:
                    self.ref[k] = False
                    self.hand += 1
                else:
                    return k
            else:
                self.hand += 1

POLICIES = {"lru": LRUPolicy, "fifo": FIFOPolicy, "clock": ClockPolicy}

# pool
class BufferPool:
    def __init__(self, capacity=256, policy="lru"):
        if policy not in POLICIES:
            raise ValueError(f"unknown policy {policy!r}; choose from {sorted(POLICIES)}")
        self.capacity = capacity
        self.policy_name = policy
        self.policy = POLICIES[policy]()
        self.files = {}        # fid -> DiskManager
        self.frames = {}       # (fid, pid) -> Frame
        self._next_fid = 0
        self.reset_stats()

    # files
    def register_file(self, disk):
        fid = self._next_fid
        self._next_fid += 1
        self.files[fid] = disk
        return fid

    def drop_file(self, fid, close=True):
        self.flush_file(fid)
        for key in [k for k in self.frames if k[0] == fid]:
            del self.frames[key]
            self.policy.on_remove(key)
        disk = self.files.pop(fid)
        if close:
            disk.close()

    # stats
    def reset_stats(self):
        self.hits = self.misses = self.evictions = self.disk_reads = self.disk_writes = 0

    @property
    def hit_ratio(self):
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def stats(self):
        return {"hits": self.hits, "misses": self.misses, "hit_ratio": self.hit_ratio,
                "evictions": self.evictions, "page_reads": self.disk_reads,
                "page_writes": self.disk_writes}

    # core
    def _make_room(self):
        if len(self.frames) < self.capacity:
            return
        key = self.policy.victim(lambda k: self.frames[k].pins == 0)
        if key is None:
            raise BufferPoolFullError("all frames are pinned")
        fr = self.frames.pop(key)
        self.policy.on_remove(key)
        if fr.dirty:
            self.files[key[0]].write_page(key[1], fr.data)
            self.disk_writes += 1
        self.evictions += 1

    def fetch(self, fid, pid):
        key = (fid, pid)
        fr = self.frames.get(key)
        if fr is not None:
            self.hits += 1
            self.policy.on_access(key)
        else:
            self.misses += 1
            self._make_room()
            fr = Frame(key, self.files[fid].read_page(pid))
            self.disk_reads += 1
            self.frames[key] = fr
            self.policy.on_insert(key)
        fr.pins += 1
        return fr

    def unpin(self, fr):
        fr.pins -= 1

    @contextmanager
    def page(self, fid, pid):
        fr = self.fetch(fid, pid)
        try:
            yield fr
        finally:
            fr.pins -= 1

    def new_page(self, fid):
        disk = self.files[fid]
        pid = disk.allocate_page()
        self._make_room()
        fr = Frame((fid, pid), bytearray(disk.page_size))
        self.frames[fr.key] = fr
        self.policy.on_insert(fr.key)
        return pid

    # flushing
    def flush_file(self, fid):
        for (f, pid), fr in self.frames.items():
            if f == fid and fr.dirty:
                self.files[f].write_page(pid, fr.data)
                self.disk_writes += 1
                fr.dirty = False
        if fid in self.files:
            self.files[fid].f.flush()

    def flush_all(self, sync=False):
        for fid in list(self.files):
            self.flush_file(fid)
            if sync:
                self.files[fid].sync()

    def clear(self):
        """Flush and empty the pool (cold cache); statistics are reset."""
        self.flush_all()
        self.frames.clear()
        self.policy = POLICIES[self.policy_name]()
        self.reset_stats()
