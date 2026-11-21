import os, random, shutil, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from Pdb import Database, BPlusTree, LSMTree, BufferPool, StreamCipherCodec, WorkloadProfile, choose_index
from Pdb.sql import SQLError 


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.pool = BufferPool(128)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def make(self, kind, ktype="INT", name="ix", **kw):
        if kind == "btree":
            return BPlusTree(os.path.join(self.dir, name + ".bpt"), ktype, self.pool, **kw)
        return LSMTree(os.path.join(self.dir, name + ".lsm"), ktype, self.pool, memtable_limit=300, max_tables=3, **kw)

class IndexCorrectness(Base):
    def run_against_model(self, kind, ktype="INT"):
        rng = random.Random(7)
        idx = self.make(kind, ktype)
        model = set()
        mk = (lambda i: i) if ktype == "INT" else (lambda i: f"k{i:05d}")
        for step in range(6000):
            k = mk(rng.randrange(1500))
            rid = (rng.randrange(50), rng.randrange(20))
            if rng.random() < 0.75:
                idx.insert(k, rid); model.add((k, rid))
            else:
                victim = rng.choice(sorted(model)) if model else None
                if victim:
                    idx.delete(victim[0], victim[1]); model.discard(victim)
        full = sorted(model)
        self.assertEqual([(k, r) for k, r in idx.range()], full)
        for _ in range(100):
            a, b = sorted((mk(rng.randrange(1500)), mk(rng.randrange(1500))))
            self.assertEqual(list(idx.range(a, b)), [(k, r) for k, r in full if a <= k <= b])
            self.assertEqual(sorted(idx.search(a)), sorted(r for k, r in full if k == a))
        return idx, full

    def test_btree_matches_model_int(self):
        idx, _ = self.run_against_model("btree")
        self.assertGreater(idx.height(), 1)

    def test_btree_matches_model_text(self):
        self.run_against_model("btree", "TEXT")

    def test_lsm_matches_model_int(self):
        idx, _ = self.run_against_model("lsm")
        self.assertGreater(idx.flushes, 0)
        self.assertGreater(idx.compactions, 0)

    def test_lsm_matches_model_text(self):
        self.run_against_model("lsm", "TEXT")

    def test_btree_persistence(self):
        idx = self.make("btree")
        for i in range(5000):
            idx.insert(i, (i // 10, i % 10))
        idx.close()
        idx = self.make("btree")
        self.assertEqual(len(idx), 5000)
        self.assertEqual(idx.search(1234), [(123, 4)])
        self.assertEqual(len(list(idx.range(100, 199))), 100)

    def test_lsm_persistence_clean_and_crash(self):
        idx = self.make("lsm")
        for i in range(1000):
            idx.insert(i, (i, 0))
        idx.delete(5, (5, 0))
        idx.close()
        idx = self.make("lsm")
        self.assertEqual(idx.search(5), [])
        self.assertEqual(idx.search(6), [(6, 0)])
        for i in range(2000, 2050):                # lives only in memtable + WAL
            idx.insert(i, (i, 0))
        idx._simulate_crash()
        idx = self.make("lsm")                     # WAL replay
        self.assertEqual(len(list(idx.range(2000, 2049))), 50)
        self.assertEqual(idx.search(999), [(999, 0)])

    def test_duplicate_keys(self):
        for kind in ("btree", "lsm"):
            idx = self.make(kind, name="dup_" + kind)
            for s in range(10):
                idx.insert(42, (1, s))
            idx.insert(42, (1, 3))                 # same entry twice -> idempotent
            self.assertEqual(len(idx.search(42)), 10)

    def test_text_key_too_long(self):
        idx = self.make("btree", "TEXT")
        with self.assertRaises(ValueError):
            idx.insert("x" * 100, (0, 0))

    def test_encrypted_index_files(self):
        codec = StreamCipherCodec("k")
        idx = BPlusTree(os.path.join(self.dir, "e.bpt"), "TEXT", self.pool, codec)
        idx.insert("plaintext-key-123", (1, 1))
        idx.close()
        with open(os.path.join(self.dir, "e.bpt"), "rb") as f:
            self.assertNotIn(b"plaintext-key-123", f.read())

class SQLIndexed(Base):
    def test_indexed_queries_equal_scan_for_both_index_kinds(self):
        for kind in ("btree", "lsm"):
            for layout in ("row", "column"):
                d = os.path.join(self.dir, f"{kind}_{layout}")
                db = Database(d)
                db.execute(f"CREATE TABLE p (id INT, cat TEXT(16), price FLOAT) USING {layout}")
                rng = random.Random(3)
                rows = [(i, rng.choice(["toys", "books", "food"]), round(rng.random() * 100, 2)) for i in range(2000)]
                db.table("p").insert_many(rows)
                db.execute(f"CREATE INDEX i1 ON p (id) USING {kind}")
                db.execute(f"CREATE INDEX i2 ON p (cat) USING {kind}")
                for sql in ["SELECT * FROM p WHERE id = 77", "SELECT id FROM p WHERE id BETWEEN 100 AND 250",
                            "SELECT * FROM p WHERE id > 1990", "SELECT * FROM p WHERE cat = 'toys' AND price < 10",
                            "SELECT id FROM p WHERE id >= 5.5 AND id < 9.2", "SELECT * FROM p WHERE id = 2.5"]:
                    via_idx = db.execute(sql)
                    scan_rows, _ = db.table("p").select(None if "id FROM" not in sql else ["id"],
                                                        parse_where(sql), use_index=False)
                    self.assertIn("INDEX", via_idx.plan, sql)
                    self.assertEqual(sorted(via_idx.rows), sorted(scan_rows), sql)
                db.close()

    def test_index_maintained_on_insert_update_delete_and_restart(self):
        db = Database(self.dir)
        db.execute("CREATE TABLE p (id INT, name TEXT)")
        db.execute("CREATE INDEX i ON p (id) USING btree")
        db.execute("INSERT INTO p VALUES (1,'a'),(2,'b'),(3,'c')")
        db.execute("UPDATE p SET id = 20 WHERE id = 2")
        db.execute("DELETE FROM p WHERE id = 3")
        self.assertEqual(db.execute("SELECT name FROM p WHERE id = 20").rows, [("b",)])
        self.assertEqual(db.execute("SELECT * FROM p WHERE id = 3").rows, [])
        db.close()
        db = Database(self.dir)
        r = db.execute("SELECT * FROM p WHERE id = 20")
        self.assertEqual(r.rows, [(20, "b")])
        self.assertIn("btree", r.plan)
        db.close()

    def test_sql_errors_and_quoting(self):
        db = Database(self.dir)
        db.execute("CREATE TABLE p (id INT, name TEXT)")
        db.execute("INSERT INTO p VALUES (1, 'O''Brien; DROP TABLE p')")
        self.assertEqual(db.execute("SELECT name FROM p WHERE id = 1").rows, [("O'Brien; DROP TABLE p",)])
        self.assertEqual(len(db.tables), 1)
        for bad in ["SELEC * FROM p", "SELECT * FROM nope", "SELECT * FROM p WHERE id = 'x'", "INSERT INTO p VALUES (1)"]:
            with self.assertRaises(Exception):
                db.execute(bad)
        db.close()

    def test_explain_and_limit(self):
        db = Database(self.dir)
        db.execute("CREATE TABLE p (id INT)")
        db.table("p").insert_many([(i,) for i in range(100)])
        self.assertIn("SEQ SCAN", db.execute("EXPLAIN SELECT * FROM p WHERE id = 5").plan)
        self.assertEqual(len(db.execute("SELECT * FROM p LIMIT 7").rows), 7)
        db.close()

def parse_where(sql):
    from Pdb.sql import parse
    return parse(sql)["where"]

class Advisor(unittest.TestCase):
    def test_rules(self):
        self.assertEqual(choose_index(WorkloadProfile(3, 1, 2))[0], "btree")          # too little data
        self.assertEqual(choose_index(WorkloadProfile(900, 50, 50))[0], "btree")      # read heavy
        self.assertEqual(choose_index(WorkloadProfile(30, 10, 960))[0], "lsm")        # write heavy
        self.assertEqual(choose_index(WorkloadProfile(10, 400, 590))[0], "btree")     # write heavy but range heavy
        self.assertEqual(choose_index(WorkloadProfile(300, 100, 400))[0], "btree")    # balanced

    def test_auto_index_uses_observed_workload(self):
        d = tempfile.mkdtemp()
        try:
            db = Database(d)
            db.execute("CREATE TABLE p (id INT)")
            t = db.table("p")
            t.insert_many([(i,) for i in range(200)])          # 200 writes -> write-heavy
            msg = db.execute("CREATE INDEX i ON p (id) USING AUTO").message
            self.assertIn("lsm", msg)
            db.close()
        finally:
            shutil.rmtree(d, ignore_errors=True)

if __name__ == "__main__":
    unittest.main()
