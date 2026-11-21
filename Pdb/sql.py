"""Parser for the SQL-like language (CREATE TABLE, CREATE INDEX, INSERT, SELECT ... WHERE, UPDATE, DELETE, EXPLAIN)."""
import re

TOKEN_RE = re.compile(
    r"\s*(?:(?P<num>-?\d+\.\d+|-?\d+)|(?P<str>'(?:[^']|'')*')|(?P<id>[A-Za-z_][A-Za-z_0-9]*)"
    r"|(?P<op><=|>=|!=|<>|[=<>(),*;]))")

TYPE_ALIASES = {"INT": "INT", "INTEGER": "INT", "BIGINT": "INT",
                "FLOAT": "FLOAT", "DOUBLE": "FLOAT", "REAL": "FLOAT",
                "TEXT": "TEXT", "VARCHAR": "TEXT", "CHAR": "TEXT"}

class SQLError(Exception):
    pass

def tokenize(sql):
    pos, out = 0, []
    while pos < len(sql):
        m = TOKEN_RE.match(sql, pos)
        if not m:
            if sql[pos:].strip() == "":
                break
            raise SQLError(f"unexpected character at position {pos}: {sql[pos:pos + 10]!r}")
        pos = m.end()
        if m.group("num") is not None:
            t = m.group("num")
            out.append(("lit", float(t) if "." in t else int(t)))
        elif m.group("str") is not None:
            out.append(("lit", m.group("str")[1:-1].replace("''", "'")))
        elif m.group("id") is not None:
            out.append(("id", m.group("id")))
        else:
            out.append(("op", m.group("op")))
    return out


class Parser:
    def __init__(self, sql):
        self.t, self.i = tokenize(sql), 0

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else ("eof", None)

    def next(self):
        tok = self.peek()
        self.i += 1
        return tok

    def accept_kw(self, *words):
        k, v = self.peek()
        if k == "id" and v.upper() in words:
            self.i += 1
            return v.upper()
        return None

    def expect_kw(self, word):
        if not self.accept_kw(word):
            raise SQLError(f"expected {word}, found {self.peek()[1]!r}")

    def accept_op(self, op):
        if self.peek() == ("op", op):
            self.i += 1
            return True
        return False

    def expect_op(self, op):
        if not self.accept_op(op):
            raise SQLError(f"expected {op!r}, found {self.peek()[1]!r}")

    def ident(self):
        k, v = self.next()
        if k != "id":
            raise SQLError(f"expected identifier, found {v!r}")
        return v

    def literal(self):
        k, v = self.next()
        if k != "lit":
            raise SQLError(f"expected literal value, found {v!r}")
        return v

    # statements
    def parse(self):
        kw = self.accept_kw("CREATE", "INSERT", "SELECT", "DELETE", "UPDATE", "EXPLAIN")
        if kw == "CREATE":
            stmt = self.create()
        elif kw == "INSERT":
            stmt = self.insert()
        elif kw == "SELECT":
            stmt = self.select()
        elif kw == "EXPLAIN":
            self.expect_kw("SELECT")
            stmt = self.select()
            stmt["explain"] = True
        elif kw == "DELETE":
            self.expect_kw("FROM")
            stmt = {"type": "delete", "table": self.ident(), "where": self.where()}
        elif kw == "UPDATE":
            stmt = self.update()
        else:
            raise SQLError("unsupported statement")
        self.accept_op(";")
        if self.peek()[0] != "eof":
            raise SQLError(f"unexpected trailing input: {self.peek()[1]!r}")
        return stmt

    def create(self):
        if self.accept_kw("TABLE"):
            name = self.ident()
            self.expect_op("(")
            cols = []
            while True:
                cname, tname = self.ident(), self.ident().upper()
                if tname not in TYPE_ALIASES:
                    raise SQLError(f"unknown type {tname}")
                width = None
                if self.accept_op("("):
                    width = self.literal()
                    self.expect_op(")")
                cols.append((cname, TYPE_ALIASES[tname], width))
                if not self.accept_op(","):
                    break
            self.expect_op(")")
            layout = "row"
            if self.accept_kw("USING"):
                w = self.ident().upper()
                if w not in ("ROW", "COLUMN", "COLUMNAR"):
                    raise SQLError("layout must be ROW or COLUMN")
                layout = "row" if w == "ROW" else "column"
            return {"type": "create_table", "name": name, "columns": cols, "layout": layout}
        self.expect_kw("INDEX")
        name = self.ident()
        self.expect_kw("ON")
        table = self.ident()
        self.expect_op("(")
        col = self.ident()
        self.expect_op(")")
        kind = "auto"
        if self.accept_kw("USING"):
            w = self.ident().upper()
            if w not in ("BTREE", "LSM", "AUTO"):
                raise SQLError("index kind must be BTREE, LSM or AUTO")
            kind = w.lower()
        return {"type": "create_index", "name": name, "table": table, "column": col, "kind": kind}

    def insert(self):
        self.expect_kw("INTO")
        table = self.ident()
        self.expect_kw("VALUES")
        rows = []
        while True:
            self.expect_op("(")
            vals = [self.literal()]
            while self.accept_op(","):
                vals.append(self.literal())
            self.expect_op(")")
            rows.append(tuple(vals))
            if not self.accept_op(","):
                break
        return {"type": "insert", "table": table, "rows": rows}

    def select(self):
        cols = None
        count = False
        if self.accept_op("*"):
            pass
        elif self.accept_kw("COUNT"):
            self.expect_op("(")
            self.expect_op("*")
            self.expect_op(")")
            count = True
        else:
            cols = [self.ident()]
            while self.accept_op(","):
                cols.append(self.ident())
        self.expect_kw("FROM")
        table = self.ident()
        where = self.where()
        limit = None
        if self.accept_kw("LIMIT"):
            limit = self.literal()
            if not isinstance(limit, int) or limit < 0:
                raise SQLError("LIMIT must be a non-negative integer")
        return {"type": "select", "table": table, "columns": cols, "count": count,
                "where": where, "limit": limit, "explain": False}

    def update(self):
        table = self.ident()
        self.expect_kw("SET")
        sets = []
        while True:
            col = self.ident()
            self.expect_op("=")
            sets.append((col, self.literal()))
            if not self.accept_op(","):
                break
        return {"type": "update", "table": table, "set": sets, "where": self.where()}

    def where(self):
        preds = []
        if not self.accept_kw("WHERE"):
            return preds
        while True:
            col = self.ident()
            if self.accept_kw("BETWEEN"):
                lo = self.literal()
                self.expect_kw("AND")
                hi = self.literal()
                preds += [(col, ">=", lo), (col, "<=", hi)]
            else:
                k, op = self.next()
                if k != "op" or op not in ("=", "!=", "<>", "<", "<=", ">", ">="):
                    raise SQLError(f"expected comparison operator, found {op!r}")
                preds.append((col, "!=" if op == "<>" else op, self.literal()))
            if not self.accept_kw("AND"):
                break
        return preds


def parse(sql):
    return Parser(sql).parse()
