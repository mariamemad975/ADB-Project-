"""Table schema and (row) record serialization."""
"""Column types and how a row is turned into bytes."""
import struct
from dataclasses import dataclass
from .config import DEFAULT_TEXT_WIDTH

INT, FLOAT, TEXT = "INT", "FLOAT", "TEXT"

@dataclass(frozen=True)
class Column:
    name: str
    type: str
    width: int = DEFAULT_TEXT_WIDTH   # TEXT only: max UTF-8 bytes (enforced by column layout and by indexes)

class Schema:
    def __init__(self, columns):
        self.columns = list(columns)
        self._pos = {c.name.lower(): i for i, c in enumerate(self.columns)}
        if len(self._pos) != len(self.columns):
            raise ValueError("duplicate column name")

    def index_of(self, name):
        try:
            return self._pos[name.lower()]
        except KeyError:
            raise KeyError(f"unknown column {name!r}") from None

    def names(self):
        return [c.name for c in self.columns]

    def coerce(self, values):
        if len(values) != len(self.columns):
            raise ValueError(f"expected {len(self.columns)} values, got {len(values)}")
        return tuple(coerce_value(c, v) for c, v in zip(self.columns, values))

    def encode(self, values):
        parts = []
        for c, v in zip(self.columns, values):
            if c.type == INT:
                parts.append(struct.pack("<q", v))
            elif c.type == FLOAT:
                parts.append(struct.pack("<d", v))
            else:
                b = v.encode("utf-8")
                parts.append(struct.pack("<H", len(b)) + b)
        return b"".join(parts)

    def decode(self, buf):
        off, out = 0, []
        for c in self.columns:
            if c.type == INT:
                out.append(struct.unpack_from("<q", buf, off)[0]); off += 8
            elif c.type == FLOAT:
                out.append(struct.unpack_from("<d", buf, off)[0]); off += 8
            else:
                n = struct.unpack_from("<H", buf, off)[0]; off += 2
                out.append(bytes(buf[off:off + n]).decode("utf-8")); off += n
        return tuple(out)

    def to_json(self):
        return [[c.name, c.type, c.width] for c in self.columns]

    @staticmethod
    def from_json(data):
        return Schema([Column(n, t, w) for n, t, w in data])

def coerce_value(col, v):
    if col.type == INT:
        if isinstance(v, bool) or not isinstance(v, int):
            raise TypeError(f"column {col.name} expects INT, got {v!r}")
        return v
    if col.type == FLOAT:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise TypeError(f"column {col.name} expects FLOAT, got {v!r}")
        return float(v)
    if col.type == TEXT:
        if not isinstance(v, str):
            raise TypeError(f"column {col.name} expects TEXT, got {v!r}")
        return v
    raise ValueError(f"unknown type {col.type}")
