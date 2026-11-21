import struct

page_size = 4096

HEADER = struct.Struct("<H")


class RowStore:
    def __init__(self, bufferpoolm, template):
        self.bufferpoolm = bufferpoolm
        self.template = template 
        self.count = 0
        self.row_size = self.template.size

    def insert(self, row_data):
        rows_in_page = (page_size - 2) // self.row_size
        page_id, slot = divmod(self.count, rows_in_page)
        
        page_bytes = self.bufferpoolm.fetch_page(page_id)
        b = bytearray(page_bytes)
        self.template.pack_into(b, 2 + (slot * self.row_size), *row_data)
        HEADER.pack_into(b, 0, slot + 1)
        
        self.bufferpoolm.write_page(page_id, bytes(b))
        self.bufferpoolm.unpin_page(page_id, is_dirty=True)
        self.count += 1

    def get_row(self, index):
        if index >= self.count: return None
        
        rows_in_page = (page_size - 2) // self.row_size
        page_id, slot = divmod(index, rows_in_page)
        
        page = self.bufferpoolm.fetch_page(page_id)
        data = self.template.unpack_from(page, 2 + (slot * self.row_size))
        
        self.bufferpoolm.unpin_page(page_id)
        return data

    def scan(self):
        rows_in_page = (page_size - 2) // self.row_size
        n_pages = (self.count // rows_in_page) + 1
        
        for page_id in range(n_pages):
            page = self.bufferpoolm.fetch_page(page_id)
            (count,) = HEADER.unpack_from(page, 0)
            
            for slot in range(count):
                yield self.template.unpack_from(page, 2 + (slot * self.row_size))
            
            self.bufferpoolm.unpin_page(page_id)



class ColumnStore:
    def __init__(self, bufferpoolm, column_def, startpage_offset):
        self.bufferpoolm = bufferpoolm
        self.cols = column_def
        self.start_offset = startpage_offset
        self.count = 0

    def insert(self, row_data):
        for i, (col_name, template) in enumerate(self.cols.items()):
            data = row_data[i]
            if isinstance(data, str): data = data.encode()[:template.size]
            
            data_size = template.size
            page = (page_size - 2) // data_size
            page_id = self.start_offset + (i * 1000) + (self.count // page)
            slot = self.count % page
            
            page_bytes = self.bufferpoolm.fetch_page(page_id)
            b = bytearray(page_bytes)
            template.pack_into(b, 2 + (slot * data_size), data)
            HEADER.pack_into(b, 0, slot + 1)
            
            self.bufferpoolm.write_page(page_id, bytes(b))
            self.bufferpoolm.unpin_page(page_id, is_dirty=True)
        self.count += 1

    def get_row(self, index):
        if index >= self.count: return None
        
        row_data = []
        for i, (col_name, template) in enumerate(self.cols.items()):
            data_size = template.size
            page = (page_size - 2) // data_size
            page_id = self.start_offset + (i * 1000) + (index // page)
            slot = index % page
            
            page = self.bufferpoolm.fetch_page(page_id)
            val = template.unpack_from(page, 2 + (slot * data_size))[0]
            row_data.append(val)
            self.bufferpoolm.unpin_page(page_id)
            
        return tuple(row_data)

    def scan_column(self, col_name):
        if col_name not in self.cols: return
        
        template = self.cols[col_name]
        col_i = list(self.cols.keys()).index(col_name)
        data_size = template.size
        page = (page_size - 2) // data_size
        n_pages = (self.count // page) + 1
        
        start_page = self.start_offset + (col_i * 1000)
        for page_id in range(start_page, start_page + n_pages):
            page = self.bufferpoolm.fetch_page(page_id)
            (count,) = HEADER.unpack_from(page, 0)
            for slot in range(count):
                yield template.unpack_from(page, 2 + (slot * data_size))[0]
            self.bufferpoolm.unpin_page(page_id)