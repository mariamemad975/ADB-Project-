"""Row-oriented layout: slotted pages (NSM), for small records (e.g. index leaf pages).  
Each page has a header, a slot array, and a free space area.  
Records grow downward from the top of the page, and slots grow upward from the bottom. 
A slot is a pair of (offset, length) for a record.  A deleted slot has offset == 0.  T
he RID is (page_id, slot).  The schema encodes/decodes records to/from bytes."""
import struct
from .config import PAGE_SIZE

HDR, SLOT = 4, 4


class RowStore:
    layout = "row"

    def __init__(self, pool, fid, schema):
        self.pool, self.fid, self.schema = pool, fid, schema

    @property
    def disk(self):
        return self.pool.files[self.fid]

    def _try_insert(self, pid, rec):
        with self.pool.page(self.fid, pid) as fr:
            buf = fr.data
            ns, fe = struct.unpack_from("<HH", buf, 0)
            if fe == 0:
                fe = PAGE_SIZE
            if fe - (HDR + SLOT * ns) < len(rec) + SLOT:
                return None
            fe -= len(rec)
            buf[fe:fe + len(rec)] = rec
            struct.pack_into("<HH", buf, 0, ns + 1, fe)
            struct.pack_into("<HH", buf, HDR + SLOT * ns, fe, len(rec))
            fr.dirty = True
            return (pid, ns)

    def insert(self, values):
        rec = self.schema.encode(values)
        if len(rec) + HDR + SLOT > PAGE_SIZE:
            raise ValueError("record larger than a page; store big payloads in the object store")
        n = self.disk.num_pages
        if n:
            rid = self._try_insert(n - 1, rec)
            if rid:
                return rid
        return self._try_insert(self.pool.new_page(self.fid), rec)

    def get(self, rid):
        pid, slot = rid
        if pid >= self.disk.num_pages:
            return None
        with self.pool.page(self.fid, pid) as fr:
            ns, _ = struct.unpack_from("<HH", fr.data, 0)
            if slot >= ns:
                return None
            off, ln = struct.unpack_from("<HH", fr.data, HDR + SLOT * slot)
            if off == 0:
                return None
            return self.schema.decode(fr.data[off:off + ln])

    def delete(self, rid):
        pid, slot = rid
        with self.pool.page(self.fid, pid) as fr:
            ns, _ = struct.unpack_from("<HH", fr.data, 0)
            if slot >= ns:
                return False
            off, _ = struct.unpack_from("<HH", fr.data, HDR + SLOT * slot)
            if off == 0:
                return False
            struct.pack_into("<HH", fr.data, HDR + SLOT * slot, 0, 0)
            fr.dirty = True
            return True

    def update(self, rid, values):
        """In place when the new record fits in the old slot; otherwise delete+insert (new RID)."""
        rec = self.schema.encode(values)
        pid, slot = rid
        with self.pool.page(self.fid, pid) as fr:
            off, ln = struct.unpack_from("<HH", fr.data, HDR + SLOT * slot)
            if off and len(rec) <= ln:
                fr.data[off:off + len(rec)] = rec
                struct.pack_into("<HH", fr.data, HDR + SLOT * slot, off, len(rec))
                fr.dirty = True
                return rid
        self.delete(rid)
        return self.insert(values)

    def scan(self):
        for pid in range(self.disk.num_pages):
            batch = []
            with self.pool.page(self.fid, pid) as fr:
                buf = fr.data
                ns, _ = struct.unpack_from("<HH", buf, 0)
                for s in range(ns):
                    off, ln = struct.unpack_from("<HH", buf, HDR + SLOT * s)
                    if off:
                        batch.append(((pid, s), self.schema.decode(buf[off:off + ln])))
            yield from batch   # page is unpinned before yielding

    def scan_columns(self, cols=None):
        """Row layout must decode whole records, then project."""
        for rid, row in self.scan():
            yield rid, (row if cols is None else tuple(row[i] for i in cols))

    def num_pages(self):
        return self.disk.num_pages
