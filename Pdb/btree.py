"""Disk-based B+ Tree stored in pages through the buffer pool (so it shares the M1 storage engine)."""
import struct
from bisect import bisect_left, bisect_right
from .disk import DiskManager
from .keycodec import KeyCodec, MIN_I, MAX_I

HEADER = 16
MAGIC = b"BPT1"
_CODES = {"INT": 1, "FLOAT": 2, "TEXT": 3}

class _Node:
    __slots__ = ("leaf", "keys", "children", "next")
    def __init__(self, leaf, keys, children=None, nxt=-1):
        self.leaf, self.keys, self.children, self.next = leaf, keys, children, nxt

class BPlusTree:
    kind = "btree"
    def __init__(self, path, ktype, pool, codec=None):
        self.kc = KeyCodec(ktype)
        self.pool = pool
        self.disk = DiskManager(path, codec)
        self.fid = pool.register_file(self.disk)
        self.ew = self.kc.width + 8
        self.max_keys = (self.disk.page_size - HEADER - 8) // (self.ew + 8)
        self._simple = self.kc.fmt is not None
        if self.disk.num_pages == 0:
            pool.new_page(self.fid)                    # meta
            root = pool.new_page(self.fid)             # empty leaf
            self.root, self.count = root, 0
            self._write(root, _Node(True, []))
            self._write_meta()
        else:
            with pool.page(self.fid, 0) as fr:
                magic, code, self.root, self.count = struct.unpack_from("<4sBqq", fr.data, 0)
            if magic != MAGIC or code != _CODES[ktype]:
                raise ValueError("not a B+ tree file of this key type")

    # node I/O
    def _write_meta(self):
        with self.pool.page(self.fid, 0) as fr:
            struct.pack_into("<4sBqq", fr.data, 0, MAGIC, _CODES[self.kc.ktype], self.root, self.count)
            fr.dirty = True

    def _read(self, pid):
        with self.pool.page(self.fid, pid) as fr:
            buf = fr.data
            leaf, n, nxt = struct.unpack_from("<BHq", buf, 0)
            if self._simple:
                vals = struct.unpack_from("<" + (self.kc.fmt + "ii") * n, buf, HEADER)
                keys = [(vals[i], vals[i + 1], vals[i + 2]) for i in range(0, 3 * n, 3)]
            else:
                keys, off = [], HEADER
                for _ in range(n):
                    k = self.kc.unpack(buf, off)
                    p, s = struct.unpack_from("<ii", buf, off + self.kc.width)
                    keys.append((k, p, s))
                    off += self.ew
            if leaf:
                return _Node(True, keys, None, nxt)
            children = list(struct.unpack_from(f"<{n + 1}q", buf, HEADER + n * self.ew))
            return _Node(False, keys, children, nxt)

    def _write(self, pid, node):
        n = len(node.keys)
        with self.pool.page(self.fid, pid) as fr:
            buf = fr.data
            struct.pack_into("<BHq", buf, 0, 1 if node.leaf else 0, n, node.next)
            if self._simple:
                flat = [x for e in node.keys for x in e]
                struct.pack_into("<" + (self.kc.fmt + "ii") * n, buf, HEADER, *flat)
            else:
                off = HEADER
                for k, p, s in node.keys:
                    buf[off:off + self.kc.width] = self.kc.pack(k)
                    struct.pack_into("<ii", buf, off + self.kc.width, p, s)
                    off += self.ew
            if not node.leaf:
                struct.pack_into(f"<{n + 1}q", buf, HEADER + n * self.ew, *node.children)
            fr.dirty = True

    # insert
    def insert(self, key, rid):
        self.kc.check(key)
        e = (key, rid[0], rid[1])
        res = self._insert(self.root, e)
        if res:
            sep, new_pid = res
            new_root = self.pool.new_page(self.fid)
            self._write(new_root, _Node(False, [sep], [self.root, new_pid]))
            self.root = new_root
        self._write_meta()

    def _insert(self, pid, e):
        node = self._read(pid)
        if node.leaf:
            i = bisect_left(node.keys, e)
            if i < len(node.keys) and node.keys[i] == e:
                return None
            node.keys.insert(i, e)
            self.count += 1
            if len(node.keys) <= self.max_keys:
                self._write(pid, node)
                return None
            mid = len(node.keys) // 2
            right = _Node(True, node.keys[mid:], None, node.next)
            node.keys = node.keys[:mid]
            new_pid = self.pool.new_page(self.fid)
            node.next = new_pid
            self._write(pid, node)
            self._write(new_pid, right)
            return right.keys[0], new_pid
        i = bisect_right(node.keys, e)
        res = self._insert(node.children[i], e)
        if not res:
            return None
        sep, new_child = res
        node.keys.insert(i, sep)
        node.children.insert(i + 1, new_child)
        if len(node.keys) <= self.max_keys:
            self._write(pid, node)
            return None
        mid = len(node.keys) // 2
        up = node.keys[mid]
        right = _Node(False, node.keys[mid + 1:], node.children[mid + 1:])
        node.keys, node.children = node.keys[:mid], node.children[:mid + 1]
        new_pid = self.pool.new_page(self.fid)
        self._write(pid, node)
        self._write(new_pid, right)
        return up, new_pid

    # delete
    def delete(self, key, rid):
        e = (key, rid[0], rid[1])
        pid = self.root
        while True:
            node = self._read(pid)
            if node.leaf:
                break
            pid = node.children[bisect_right(node.keys, e)]
        i = bisect_left(node.keys, e)
        if i < len(node.keys) and node.keys[i] == e:
            del node.keys[i]
            self._write(pid, node)
            self.count -= 1
            self._write_meta()
            return True
        return False

    # search
    def range(self, lo=None, hi=None):
        """Yield (key, rid) for lo <= key <= hi (either bound may be None), in key order."""
        probe = None if lo is None else (lo, MIN_I, MIN_I)
        pid = self.root
        while True:
            node = self._read(pid)
            if node.leaf:
                break
            pid = node.children[0] if probe is None else node.children[bisect_right(node.keys, probe)]
        start = 0 if probe is None else bisect_left(node.keys, probe)
        while True:
            for k, p, s in node.keys[start:]:
                if hi is not None and k > hi:
                    return
                yield k, (p, s)
            if node.next < 0:
                return
            node = self._read(node.next)
            start = 0

    def search(self, key):
        return [rid for _, rid in self.range(key, key)]

    def height(self):
        h, pid = 1, self.root
        while True:
            node = self._read(pid)
            if node.leaf:
                return h
            pid, h = node.children[0], h + 1

    def __len__(self):
        return self.count

    def size_bytes(self):
        return self.disk.size_bytes()

    def flush(self):
        self._write_meta()
        self.pool.flush_file(self.fid)

    def close(self):
        self.flush()
        self.pool.drop_file(self.fid)
