import os
import struct
import time

from buffer_pool import BufferPoolManager
from disk_manager import DiskManager
from replacers import LRU, FIFO
from storage_type import RowStore, ColumnStore

row_temp = struct.Struct("<ii")
col_defs = {"price": struct.Struct("<i"), "products": struct.Struct("<i")}

def correctness_test():
    print("Testing Correctness:")
    file = "correctness.bin"
    dm = DiskManager(file)
    bpm = BufferPoolManager(dm, 2, LRU())
    
    rs = RowStore(bpm, row_temp)
    rs.insert((10, 20))
    bpm.flush_all()
    dm.close()
    
    dm = DiskManager(file)
    bpm = BufferPoolManager(dm, 2, LRU())
    rs = RowStore(bpm, row_temp)
    assert rs.get_row(0) == (10, 20), "Persistence failed"
    print("Persistence and RowStore work")

    cs = ColumnStore(bpm, col_defs, 1000)
    cs.insert((100, 200))
    assert list(cs.scan_column("price")) == [100], "ColumnStore failed"
    print("ColumnStore works")
    
    dm.close()
    os.remove(file)

def run_benchmarks():
    print("\nRunning Benchmarks:")
    file = "benchmarks.bin"
    
    pages = [0, 1, 0, 2, 0]
    for replacer in [LRU, FIFO]:
        dm = DiskManager(file)
        bpm = BufferPoolManager(dm, 2, replacer())
        for p in pages:
            bpm.fetch_page(p)
            bpm.unpin_page(p)
        stats = bpm.hit_ratio()
        print(f"{replacer.__name__} Hit Ratio: {stats['hit_ratio']}")
        dm.close()

    dm = DiskManager(file)
    bpm = BufferPoolManager(dm, 100, LRU())
    rs = RowStore(bpm, row_temp)
    cs = ColumnStore(bpm, col_defs, 2000)

    for i in range(1000):
        rs.insert((i, i))
        cs.insert((i, i))

    start = time.time()
    list(rs.scan())
    row_time = time.time() - start

    start = time.time()
    list(cs.scan_column("price"))
    col_time = time.time() - start

    print(f"Row scan time: {row_time}s")
    print(f"Col scan time: {col_time}s")
    
    dm.close()
    os.remove(file)

if __name__ == "__main__":
    correctness_test()
    run_benchmarks()