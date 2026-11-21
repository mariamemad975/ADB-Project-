"""Column-oriented layout (DSM): one paged file per column + a 1-byte 'live' flag file."""
import struct
from .config import PAGE_SIZE
from .schema import INT, FLOAT

HDR = 4
class ColumnStore:
    layout = "column"

    def __init__(self, pool, fids, schema):
        self.pool, self.fids, self.schema = pool, list(fids), schema
        self.widths = [8 if c.type in (INT, FLOAT) else c.width for c in schema.columns] + [1]
        self.caps = [(PAGE_SIZE - HDR) // w for w in self.widths]
        self.nrows = self._recover_nrows()

    def _enc(self, ci, v):
        c = self.schema.columns[ci]
        if c.type == INT:
            return struct.pack("<q", v)
        if c.type == FLOAT:
            return struct.pack("<d", v)
        b = v.encode("utf-8")
        if len(b) > c.width:
            raise ValueError(f"TEXT value for {c.name} exceeds {c.width} bytes in column layout")
        return b.ljust(c.width, b"\0")

    def _dec(self, ci, raw):
        c = self.schema.columns[ci]
        if c.type == INT:
            return struct.unpack("<q", raw)[0]
        if c.type == FLOAT:
            return struct.unpack("<d", raw)[0]
        return bytes(raw).rstrip(b"\0").decode("utf-8")

    def _recover_nrows(self):
        flag = self.fids[-1]
        n = self.pool.files[flag].num_pages
        if n == 0:
            return 0
        with self.pool.page(flag, n - 1) as fr:
            cnt = struct.unpack_from("<I", fr.data, 0)[0]
        return (n - 1) * self.caps[-1] + cnt

    def _write_cell(self, ci, rowid, raw):
        fid, cap, w = self.fids[ci], self.caps[ci], self.widths[ci]
        pid = rowid // cap
        while self.pool.files[fid].num_pages <= pid:
            self.pool.new_page(fid)
        with self.pool.page(fid, pid) as fr:
            slot = rowid % cap
            off = HDR + slot * w
            fr.data[off:off + w] = raw
            if slot + 1 > struct.unpack_from("<I", fr.data, 0)[0]:
                struct.pack_into("<I", fr.data, 0, slot + 1)
            fr.dirty = True

    def _read_cell(self, ci, rowid):
        fid, cap, w = self.fids[ci], self.caps[ci], self.widths[ci]
        with self.pool.page(fid, rowid // cap) as fr:
            off = HDR + (rowid % cap) * w
            return bytes(fr.data[off:off + w])

    def _read_range(self, ci, a, b):
        """Decoded values of column ci for rows [a, b)."""
        fid, cap, w = self.fids[ci], self.caps[ci], self.widths[ci]
        ctype = self.schema.columns[ci].type if ci < len(self.schema.columns) else "FLAG"
        out = []
        for pid in range(a // cap, (b - 1) // cap + 1):
            with self.pool.page(fid, pid) as fr:
                cnt = struct.unpack_from("<I", fr.data, 0)[0]
                start, end = max(a, pid * cap), min(b, pid * cap + cnt)
                n = end - start
                if n <= 0:
                    continue
                off = HDR + (start - pid * cap) * w
                if ctype == INT:
                    out.extend(struct.unpack_from(f"<{n}q", fr.data, off))
                elif ctype == FLOAT:
                    out.extend(struct.unpack_from(f"<{n}d", fr.data, off))
                elif ctype == "FLAG":
                    out.extend(fr.data[off:off + n])
                else:
                    out.extend(bytes(fr.data[off + i * w: off + (i + 1) * w]).rstrip(b"\0").decode("utf-8")
                               for i in range(n))
        return out

    def insert(self, values):
        rowid = self.nrows
        for ci, v in enumerate(values):
            self._write_cell(ci, rowid, self._enc(ci, v))
        self._write_cell(len(self.widths) - 1, rowid, b"\x01")
        self.nrows += 1
        return (rowid, 0)

    def _live(self, rowid):
        return 0 <= rowid < self.nrows and self._read_cell(len(self.widths) - 1, rowid) == b"\x01"

    def get(self, rid):
        rowid = rid[0]
        if not self._live(rowid):
            return None
        return tuple(self._dec(ci, self._read_cell(ci, rowid)) for ci in range(len(self.schema.columns)))

    def delete(self, rid):
        if not self._live(rid[0]):
            return False
        self._write_cell(len(self.widths) - 1, rid[0], b"\x00")
        return True

    def update(self, rid, values):
        encoded = [self._enc(ci, v) for ci, v in enumerate(values)]   # validate before writing
        for ci, raw in enumerate(encoded):
            self._write_cell(ci, rid[0], raw)
        return rid

    def scan_columns(self, cols=None):
        cols = list(range(len(self.schema.columns))) if cols is None else list(cols)
        block = self.caps[-1]
        for a in range(0, self.nrows, block):
            b = min(a + block, self.nrows)
            flags = self._read_range(len(self.widths) - 1, a, b)
            data = [self._read_range(ci, a, b) for ci in cols]
            for i, live in enumerate(flags):
                if live == 1:
                    yield (a + i, 0), tuple(d[i] for d in data)

    def scan(self):
        return self.scan_columns(None)

    def num_pages(self):
        return sum(self.pool.files[f].num_pages for f in self.fids)
