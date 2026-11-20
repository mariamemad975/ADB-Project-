
class BufferPoolManager:
    def __init__(self, disk_manager, pool_size, replacer):
        self.disk_manager = disk_manager
        self.size = pool_size
        self.replacer = replacer
        self.page_table = {}
        self.frames = [None] * pool_size
        self.pin_count = [0] * pool_size
        self.dirty = [False] * pool_size
        self.frame_to_page = {}
        
        self.hits = 0
        self.misses = 0

    def fetch_page(self, page_id):
        if page_id in self.page_table:
            self.hits +=1
            id = self.page_table[page_id]
            self.replacer.record_access(id)
            self.pin_count[id] +=1
            self.replacer.set_evictable(id, False)
            return self.frames[id]
        else:
            self.misses += 1
            if len(self.page_table) >= self.size:
                id = self.replacer.evict()
                old_pageid = self.frame_to_page[id]
                if self.dirty[id]:
                    self.disk_manager.write(old_pageid, bytes(self.frames[id]))
                    self.dirty[id] = False
                del self.page_table[old_pageid]
            else:
                id = len(self.page_table)
            self.frames[id] = self.disk_manager.read(page_id)
            self.page_table[page_id] = id
            self.frame_to_page[id] = page_id
            self.replacer.record_access(id)
            self.pin_count[id] +=1
            self.replacer.set_evictable(id, False)
            return self.frames[id]

    def write_page(self, page_id, data):
        if page_id not in self.page_table:
            self.fetch_page(page_id)

        id = self.page_table[page_id]
        self.frames[id] = data
        self.replacer.record_access(id)
        self.dirty[id] = True

    def unpin_page(self, page_id, is_dirty=False):
        id = self.page_table[page_id]
        if is_dirty:
            self.dirty[id] = True
        self.pin_count[id] -= 1
        if self.pin_count[id] == 0:
            self.replacer.set_evictable(id, True)

    def flush_all(self):
        for page_id, id in self.page_table.items():
            if self.dirty[id]:
                self.disk_manager.write(page_id, bytes(self.frames[id]))
                self.dirty[id] = False

    def hit_ratio(self):
        total = self.hits + self.misses
        r = self.hits / total if total else 0.0 
        return  {"hits": self.hits, "misses": self.misses, "hit_ratio": r}