import os

page_size = 4096

class DiskManager:
    def __init__(self, file):
        mode = "r+b" if os.path.exists(file) else "w+b"
        self.f = open(file, mode)

    def read(self, page_id):
        self.f.seek(page_id * page_size)
        r = self.f.read(page_size)
        return r.ljust(page_size)

    def write(self, page_id, data):
        data = data.ljust(page_size, b"\0")[:page_size]
        self.f.seek(page_id * page_size)
        self.f.write(data)
        self.f.flush()

    def close(self):
        self.f.close()