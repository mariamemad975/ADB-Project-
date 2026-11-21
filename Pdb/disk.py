"""DiskManager: a file of fixed-size pages. All durable I/O goes through here."""
import os
from .config import PAGE_SIZE
from .encryption import NullCodec

class DiskManager:
    def __init__(self, path, codec=None, page_size=PAGE_SIZE):
        self.path = path
        self.page_size = page_size
        self.codec = codec or NullCodec()
        self.ctx = os.path.basename(path)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.f = open(path, "r+b" if os.path.exists(path) else "w+b")
        self.f.seek(0, 2)
        self.num_pages = self.f.tell() // page_size
        self.reads = 0
        self.writes = 0

    def read_page(self, pid):
        if not 0 <= pid < self.num_pages:
            raise IndexError(f"page {pid} out of range in {self.ctx}")
        self.f.seek(pid * self.page_size)
        raw = self.f.read(self.page_size)
        self.reads += 1
        return bytearray(self.codec.decrypt(self.ctx, pid, raw))

    def write_page(self, pid, data, count=True):
        assert len(data) == self.page_size
        self.f.seek(pid * self.page_size)
        self.f.write(self.codec.encrypt(self.ctx, pid, bytes(data)))
        if count:
            self.writes += 1

    def allocate_page(self):
        pid = self.num_pages
        self.num_pages += 1
        self.write_page(pid, bytes(self.page_size), count=False)
        return pid

    def size_bytes(self):
        return self.num_pages * self.page_size

    def sync(self):
        self.f.flush()
        os.fsync(self.f.fileno())

    def close(self):
        if not self.f.closed:
            self.sync()
            self.f.close()
