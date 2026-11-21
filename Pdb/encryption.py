"""Encryption extension point (M2 'encryption hook').

Every page that leaves/enters the engine passes through a *codec*:
    encrypt(ctx, page_id, data) -> bytes   (same length as input)
    decrypt(ctx, page_id, data) -> bytes
`ctx` is the file name, so identical pages in different files differ on disk."""
import hashlib

class NullCodec:
    name = "none"

    def encrypt(self, ctx, page_id, data):
        return bytes(data)

    def decrypt(self, ctx, page_id, data):
        return bytes(data)


class StreamCipherCodec:
    name = "sha256-ctr-demo"

    def __init__(self, key):
        self.key = key.encode() if isinstance(key, str) else bytes(key)

    def _keystream(self, ctx, page_id, n):
        out, counter = bytearray(), 0
        prefix = self.key + ctx.encode() + page_id.to_bytes(8, "little")
        while len(out) < n:
            out += hashlib.sha256(prefix + counter.to_bytes(4, "little")).digest()
            counter += 1
        return bytes(out[:n])

    def _xor(self, ctx, page_id, data):
        data = bytes(data)
        n = len(data)
        ks = self._keystream(ctx, page_id, n)
        return (int.from_bytes(data, "little") ^ int.from_bytes(ks, "little")).to_bytes(n, "little")

    def encrypt(self, ctx, page_id, data):
        return self._xor(ctx, page_id, data)

    decrypt = encrypt
