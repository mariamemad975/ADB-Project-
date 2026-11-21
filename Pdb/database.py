"""Database: catalog + shared buffer pool + tables + object store + SQL execution."""
import json
import os
from .btree import BPlusTree
from .buffer_pool import BufferPool
from .column_store import ColumnStore
from .disk import DiskManager
from .lsm import LSMTree
from .object_store import ObjectStore
from .row_store import RowStore
from .schema import Column, Schema, DEFAULT_TEXT_WIDTH
from .sql import parse, SQLError
from .table import Table

class Result:
    def __init__(self, columns=None, rows=None, message="", plan=None):
        self.columns, self.rows, self.message, self.plan = columns or [], rows or [], message, plan

    def __iter__(self):
        return iter(self.rows)

    def __len__(self):
        return len(self.rows)

    def __str__(self):
        out = []
        if self.plan:
            out.append(f"[plan] {self.plan}")
        if self.columns:
            cells = [[str(v) for v in r] for r in self.rows[:50]]
            widths = [max([len(c)] + [len(r[i]) for r in cells]) for i, c in enumerate(self.columns)]
            out.append(" | ".join(c.ljust(w) for c, w in zip(self.columns, widths)))
            out.append("-+-".join("-" * w for w in widths))
            out += [" | ".join(v.ljust(w) for v, w in zip(r, widths)) for r in cells]
            if len(self.rows) > 50:
                out.append(f"... ({len(self.rows)} rows total)")
            else:
                out.append(f"({len(self.rows)} rows)")
        if self.message:
            out.append(self.message)
        return "\n".join(out)

class Database:
    def __init__(self, path, buffer_frames=256, policy="lru", codec=None):
        self.path, self.codec = path, codec
        os.makedirs(os.path.join(path, "data"), exist_ok=True)
        os.makedirs(os.path.join(path, "indexes"), exist_ok=True)
        self.pool = BufferPool(buffer_frames, policy)
        self.tables = {}
        self.objects = ObjectStore(self.pool, DiskManager(os.path.join(path, "data", "objects.dat"), codec))
        self._catalog_path = os.path.join(path, "catalog.json")
        self._loading = True
        if os.path.exists(self._catalog_path):
            self._load_catalog()
        self._loading = False

    # catalog
    def _save_catalog(self):
        if self._loading:
            return
        cat = {}
        for name, t in self.tables.items():
            cat[name] = {"columns": t.schema.to_json(), "layout": t.layout,
                         "indexes": [{"name": n, "column": c, "kind": k} for c, (n, k, _) in t.indexes.items()]}
        tmp = self._catalog_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cat, f, indent=1)
        os.replace(tmp, self._catalog_path)

    def _load_catalog(self):
        with open(self._catalog_path) as f:
            cat = json.load(f)
        for name, meta in cat.items():
            t = self._build_table(name, Schema.from_json(meta["columns"]), meta["layout"])
            for ix in meta["indexes"]:
                col = t.schema.columns[t.schema.index_of(ix["column"])]
                t.indexes[ix["column"]] = (ix["name"], ix["kind"], self._open_index(name, col, ix["kind"]))

    def _build_table(self, name, schema, layout):
        d = os.path.join(self.path, "data")
        if layout == "row":
            fid = self.pool.register_file(DiskManager(os.path.join(d, f"{name}.tbl"), self.codec))
            store = RowStore(self.pool, fid, schema)
        else:
            fids = [self.pool.register_file(DiskManager(os.path.join(d, f"{name}.c{i}.col"), self.codec))
                    for i in range(len(schema.columns))]
            fids.append(self.pool.register_file(DiskManager(os.path.join(d, f"{name}.live.col"), self.codec)))
            store = ColumnStore(self.pool, fids, schema)
        t = Table(self, name.lower(), schema, store)
        self.tables[name.lower()] = t
        return t

    def _open_index(self, table, col, kind):
        base = os.path.join(self.path, "indexes", f"{table}.{col.name.lower()}")
        if kind == "btree":
            return BPlusTree(base + ".bpt", col.type, self.pool, self.codec)
        if kind == "lsm":
            return LSMTree(base + ".lsm", col.type, self.pool, self.codec)
        raise ValueError(f"unknown index kind {kind}")

    # API
    def create_table(self, name, columns, layout="row"):
        """columns: list of (name, type[, width])."""
        if name.lower() in self.tables:
            raise ValueError(f"table {name} exists")
        cols = [Column(c[0], c[1].upper(), (c[2] if len(c) > 2 and c[2] else DEFAULT_TEXT_WIDTH)) for c in columns]
        t = self._build_table(name, Schema(cols), layout)
        self._save_catalog()
        return t

    def table(self, name):
        try:
            return self.tables[name.lower()]
        except KeyError:
            raise SQLError(f"no such table: {name}") from None

    def execute(self, sql):
        s = parse(sql)
        kind = s["type"]
        if kind == "create_table":
            self.create_table(s["name"], s["columns"], s["layout"])
            return Result(message=f"table {s['name']} created ({s['layout']} layout)")
        if kind == "create_index":
            k, why = self.table(s["table"]).create_index(s["name"], s["column"], s["kind"])
            return Result(message=f"index {s['name']} created: {k} ({why})")
        t = self.table(s["table"])
        if kind == "insert":
            t.insert_many(s["rows"])
            return Result(message=f"{len(s['rows'])} row(s) inserted")
        if kind == "select":
            if s["count"]:
                rows, plan = t.select([t.schema.columns[0].name], s["where"], None)
                return Result(["count"], [(len(rows),)], plan=plan) if not s["explain"] else Result(plan=plan)
            rows, plan = t.select(s["columns"], s["where"], s["limit"])
            if s["explain"]:
                return Result(plan=plan)
            names = s["columns"] or t.schema.names()
            return Result(names, rows, plan=plan)
        if kind == "update":
            return Result(message=f"{t.update_where(s['set'], s['where'])} row(s) updated")
        if kind == "delete":
            return Result(message=f"{t.delete_where(s['where'])} row(s) deleted")
        raise SQLError("unsupported statement")

    def flush(self):
        for t in self.tables.values():
            for _, _, idx in t.indexes.values():
                idx.flush()
        self.pool.flush_all(sync=True)

    def close(self):
        for t in self.tables.values():
            for _, _, idx in t.indexes.values():
                idx.close()
        self.pool.flush_all()
        for fid in list(self.pool.files):
            self.pool.drop_file(fid)
        self._save_catalog()
