from collections import OrderedDict

class LRU:
    def __init__(self):
        self.frame_order = OrderedDict()

    def record_access(self, id):
        if id in self.frame_order:
            self.frame_orderorder.move_to_end(id)
        else:
            self.frame_order[id] = False

    def set_evictable(self, id, status):
        if id in self.frame_order:
            self.frame_order[id] = status

    def evict(self):
        for id, status in self.frame_order.items():
            if status == True:
                id, s = self.frame_order.popitem(last=False)
                return id
        return None


class FIFO:
    def __init__(self):
        self.frame_order = OrderedDict()

    def record_access(self, id):
        if id not in self.frame_order:
            self.frame_order[id] = False

    def set_evictable(self, id, status):
        if id in self.frame_order:
            self.frame_order[id] = status

    def evict(self):
        for id, status in self.frame_order.items():
            if status == True:
                id, s = self.frame_order.popitem(last=False)
                return id
        return None