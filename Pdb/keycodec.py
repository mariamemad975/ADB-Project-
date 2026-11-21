"""Turns index keys (INT, FLOAT, TEXT) into fixed-size bytes. Both trees use it."""
import struct

INT, FLOAT, TEXT = "INT", "FLOAT", "TEXT"
MIN_I, MAX_I = -2**31, 2**31 - 1     # RID components are int32; used to build probe tuples
MAX_TEXT_KEY = 64

class KeyCodec:
    def __init__(self, ktype, max_text=MAX_TEXT_KEY):
        self.ktype, self.max_text = ktype, max_text
        if ktype == INT:
            self.width, self.fmt = 8, "q"
        elif ktype == FLOAT:
            self.width, self.fmt = 8, "d"
        elif ktype == TEXT:
            self.width, self.fmt = 2 + max_text, None
        else:
            raise ValueError(f"cannot index type {ktype}")

    def pack(self, key):
        if self.ktype == TEXT:
            b = key.encode("utf-8")
            if len(b) > self.max_text:
                raise ValueError(f"index key longer than {self.max_text} bytes")
            return struct.pack("<H", len(b)) + b.ljust(self.max_text, b"\0")
        return struct.pack("<" + self.fmt, key)

    def unpack(self, buf, off):
        if self.ktype == TEXT:
            n = struct.unpack_from("<H", buf, off)[0]
            return bytes(buf[off + 2: off + 2 + n]).decode("utf-8")
        return struct.unpack_from("<" + self.fmt, buf, off)[0]

    def check(self, key):
        if self.ktype == INT and (isinstance(key, bool) or not isinstance(key, int)):
            raise TypeError("INT index needs int keys")
        if self.ktype == FLOAT and not isinstance(key, (int, float)):
            raise TypeError("FLOAT index needs numeric keys")
        if self.ktype == TEXT and not isinstance(key, str):
            raise TypeError("TEXT index needs str keys")
