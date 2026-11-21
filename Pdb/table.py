"""Table = schema + storage layout (row/column) + secondary indexes + a tiny access-path planner."""
import math
import operator
from .index_selection import WorkloadProfile, choose_index
from .schema import INT, FLOAT, TEXT, coerce_value

OPS = {"=": operator.eq, "!=": operator.ne, "<": operator.lt, "<=": operator.le,
       ">": operator.gt, ">=": operator.ge}

class Table:
    def __init__(self, db, name, schema, store):
        self.db, self.name, self.schema, self.store = db, name, schema, store
        self.indexes = {}        # column(lower) -> (index_name, kind, index object)
        self.workload = WorkloadProfile()

    @property
    def layout(self):
        return self.store.layout

    # predicates
    def _check_preds(self, preds):
        out = []
        for col, op, val in preds:
            c = self.schema.columns[self.schema.index_of(col)]
            if op not in OPS:
                raise ValueError(f"bad operator {op}")
            if c.type == TEXT and not isinstance(val, str):
                raise TypeError(f"{c.name} is TEXT; compare it with a string")
            if c.type in (INT, FLOAT) and (isinstance(val, bool) or not isinstance(val, (int, float))):
                raise TypeError(f"{c.name} is numeric; compare it with a number")
            out.append((c.name.lower(), op, val))
        return out

    def _filter(self, preds, positions):
        checks = [(positions[c], OPS[op], v) for c, op, v in preds]
        return lambda row: all(f(row[i], v) for i, f, v in checks)

    # planning
    def plan(self, preds):
        """Choose an access path. Returns a dict describing it."""
        eq = [p for p in preds if p[1] == "=" and p[0] in self.indexes]
        if eq:
            col, _, val = eq[0]
            return {"path": "index_point", "column": col, "lo": val, "hi": val}
        by_col = {}
        for col, op, val in preds:
            if col in self.indexes and op in ("<", "<=", ">", ">="):
                lo, hi = by_col.get(col, (None, None))
                if op in (">", ">="):
                    lo = val if lo is None else max(lo, val)
                else:
                    hi = val if hi is None else min(hi, val)
                by_col[col] = (lo, hi)
        if by_col:
            col, (lo, hi) = next(iter(by_col.items()))
            return {"path": "index_range", "column": col, "lo": lo, "hi": hi}
        return {"path": "seq_scan"}

    def describe(self, plan, cols=None):
        if plan["path"] == "seq_scan":
            extra = f", reads columns {cols}" if self.layout == "column" and cols is not None else ""
            return f"SEQ SCAN on {self.name} ({self.layout} layout{extra})"
        name, kind, _ = self.indexes[plan["column"]]
        what = "POINT LOOKUP" if plan["path"] == "index_point" else "RANGE SCAN"
        return f"INDEX {what} on {self.name}.{plan['column']} using {kind} index '{name}' (lo={plan['lo']}, hi={plan['hi']})"

    def _index_bounds(self, plan):
        """Coerce probe bounds to the index key type (INT index with float literal -> ceil/floor)."""
        c = self.schema.columns[self.schema.index_of(plan["column"])]
        lo, hi = plan["lo"], plan["hi"]
        if c.type == INT:
            lo = None if lo is None else math.ceil(lo)
            hi = None if hi is None else math.floor(hi)
            if plan["path"] == "index_point" and plan["lo"] != lo:
                return "empty"
        elif c.type == FLOAT:
            lo = None if lo is None else float(lo)
            hi = None if hi is None else float(hi)
        return lo, hi

    # select
    def _candidates(self, plan, positions_cols=None):
        """Yield (rid, full_row) for the chosen access path (before residual filtering)."""
        if plan["path"] == "seq_scan":
            yield from self.store.scan_columns(positions_cols)
            return
        bounds = self._index_bounds(plan)
        if bounds == "empty":
            return
        idx = self.indexes[plan["column"]][2]
        for _, rid in idx.range(*bounds):
            row = self.store.get(rid)
            if row is not None:
                yield rid, row

    def select(self, columns=None, where=(), limit=None, use_index=True):
        """Return (rows, plan_description)."""
        preds = self._check_preds(where)
        proj = list(range(len(self.schema.columns))) if columns is None else [self.schema.index_of(c) for c in columns]
        plan = self.plan(preds) if use_index else {"path": "seq_scan"}
        if any(p[1] == "=" for p in preds):
            self.workload.point_reads += 1
        else:
            self.workload.range_reads += 1
        rows = []
        if plan["path"] == "seq_scan":
            need = sorted(set(proj) | {self.schema.index_of(c) for c, _, _ in preds})
            positions = {self.schema.columns[i].name.lower(): k for k, i in enumerate(need)}
            keep = self._filter(preds, positions)
            pos_proj = [need.index(i) for i in proj]
            for _, row in self.store.scan_columns(need):
                if keep(row):
                    rows.append(tuple(row[i] for i in pos_proj))
                    if limit is not None and len(rows) >= limit:
                        break
            desc = self.describe(plan, [self.schema.columns[i].name for i in need])
        else:
            positions = {c.name.lower(): i for i, c in enumerate(self.schema.columns)}
            keep = self._filter(preds, positions)
            for _, row in self._candidates(plan):
                if keep(row):
                    rows.append(tuple(row[i] for i in proj))
                    if limit is not None and len(rows) >= limit:
                        break
            desc = self.describe(plan)
        return rows, desc

    def matching(self, where):
        """(rid,row) list for UPDATE/DELETE."""
        preds = self._check_preds(where)
        plan = self.plan(preds)
        positions = {c.name.lower(): i for i, c in enumerate(self.schema.columns)}
        keep = self._filter(preds, positions)
        return [(rid, row) for rid, row in self._candidates(plan) if keep(row)]

    # writes
    def insert(self, values):
        row = self.schema.coerce(values)
        rid = self.store.insert(row)
        for col, (_, _, idx) in self.indexes.items():
            idx.insert(row[self.schema.index_of(col)], rid)
        self.workload.writes += 1
        return rid

    def insert_many(self, rows):
        return [self.insert(r) for r in rows]

    def get(self, rid):
        return self.store.get(rid)

    def delete_rid(self, rid):
        row = self.store.get(rid)
        if row is None:
            return False
        for col, (_, _, idx) in self.indexes.items():
            idx.delete(row[self.schema.index_of(col)], rid)
        self.store.delete(rid)
        self.workload.writes += 1
        return True

    def delete_where(self, where):
        victims = self.matching(where)
        for rid, _ in victims:
            self.delete_rid(rid)
        return len(victims)

    def update_where(self, sets, where):
        targets = self.matching(where)
        changes = [(self.schema.index_of(c), coerce_value(self.schema.columns[self.schema.index_of(c)], v))
                   for c, v in sets]
        for rid, old in targets:
            new = list(old)
            for i, v in changes:
                new[i] = v
            new = tuple(new)
            for col, (_, _, idx) in self.indexes.items():
                idx.delete(old[self.schema.index_of(col)], rid)
            new_rid = self.store.update(rid, new)
            for col, (_, _, idx) in self.indexes.items():
                idx.insert(new[self.schema.index_of(col)], new_rid)
            self.workload.writes += 1
        return len(targets)

    # index
    def create_index(self, name, column, kind="auto", profile=None):
        col = self.schema.columns[self.schema.index_of(column)]
        key = col.name.lower()
        if key in self.indexes:
            raise ValueError(f"column {column} already has an index")
        reason = "explicit"
        if kind == "auto":
            kind, reason = choose_index(profile or self.workload)
        idx = self.db._open_index(self.name, col, kind)
        ci = self.schema.index_of(column)
        for rid, row in self.store.scan_columns([ci]):
            idx.insert(row[0], rid)
        self.indexes[key] = (name, kind, idx)
        self.db._save_catalog()
        return kind, reason

    def count(self):
        return sum(1 for _ in self.store.scan_columns([0]))
