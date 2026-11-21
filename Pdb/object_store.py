"""Object (blob) storage on top of the page layer (images,..)."""
"""Stores large objects (images, documents) as chains of pages. It has put, get and delete, and it reuses freed pages."""

import struct
from .config import PAGE_SIZE

CH = 12
CAP = PAGE_SIZE - CH

class ObjectStore:
    def __init__(self, pool, disk):
        self.pool, self.disk = pool, disk
        self.fid = pool.register_file(disk)
        if disk.num_pages == 0:
            pool.new_page(self.fid)
            self._set_free_head(-1)

    def _free_head(self):
        with self.pool.page(self.fid, 0) as fr:
            return struct.unpack_from("<q", fr.data, 0)[0]

    def _set_free_head(self, v):
        with self.pool.page(self.fid, 0) as fr:
            struct.pack_into("<q", fr.data, 0, v)
            fr.dirty = True

    def _alloc(self):
        head = self._free_head()
        if head == -1:
            return self.pool.new_page(self.fid)
        with self.pool.page(self.fid, head) as fr:
            nxt = struct.unpack_from("<q", fr.data, 0)[0]
        self._set_free_head(nxt)
        return head

    def put(self, data: bytes) -> str:
        chunks = [data[i:i + CAP] for i in range(0, len(data), CAP)] or [b""]
        pids = [self._alloc() for _ in chunks]
        for i, (pid, ch) in enumerate(zip(pids, chunks)):
            nxt = pids[i + 1] if i + 1 < len(pids) else -1
            with self.pool.page(self.fid, pid) as fr:
                struct.pack_into("<qI", fr.data, 0, nxt, len(ch))
                fr.data[CH:CH + len(ch)] = ch
                fr.dirty = True
        return str(pids[0])

    def get(self, oid: str) -> bytes:
        pid, out = int(oid), []
        while pid != -1:
            with self.pool.page(self.fid, pid) as fr:
                nxt, ln = struct.unpack_from("<qI", fr.data, 0)
                out.append(bytes(fr.data[CH:CH + ln]))
            pid = nxt
        return b"".join(out)

    def delete(self, oid: str):
        first = pid = last = int(oid)
        while pid != -1:
            last = pid
            with self.pool.page(self.fid, pid) as fr:
                pid = struct.unpack_from("<q", fr.data, 0)[0]
        with self.pool.page(self.fid, last) as fr:
            struct.pack_into("<q", fr.data, 0, self._free_head())
            fr.dirty = True
        self._set_free_head(first)

    def size_bytes(self):
        return self.disk.size_bytes()
