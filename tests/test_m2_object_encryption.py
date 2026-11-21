import os, random, shutil, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from Pdb import Database, StreamCipherCodec
from Pdb.buffer_pool import BufferPool, POLICIES
from Pdb.disk import DiskManager

class TmpDirTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

class ObjectAndEncryptionTests(TmpDirTest):
    def test_object_store_roundtrip_free_list_and_restart(self):
        db = Database(self.dir)
        blobs = [os.urandom(n) for n in (0, 10, 4084, 4085, 20000)]
        oids = [db.objects.put(b) for b in blobs]
        for oid, b in zip(oids, blobs):
            self.assertEqual(db.objects.get(oid), b)
        pages_before = db.objects.disk.num_pages
        db.objects.delete(oids[-1])
        again = db.objects.put(os.urandom(20000))
        self.assertEqual(db.objects.disk.num_pages, pages_before)     # freed pages were reused
        db.close()
        db = Database(self.dir)
        self.assertEqual(db.objects.get(oids[2]), blobs[2])
        db.close()

    def test_encryption_hook_roundtrip_and_ciphertext_on_disk(self):
        secret = "TOP-SECRET-CUSTOMER-NAME"
        codec = StreamCipherCodec("k3y")
        db = Database(self.dir, codec=codec)
        t = db.create_table("c", [("id", "INT"), ("name", "TEXT", 40)], "row")
        t.insert((1, secret))
        oid = db.objects.put(secret.encode() * 10)
        db.close()
        for fn in os.listdir(os.path.join(self.dir, "data")):
            with open(os.path.join(self.dir, "data", fn), "rb") as f:
                self.assertNotIn(secret.encode(), f.read())
        db = Database(self.dir, codec=codec)
        self.assertEqual(db.table("c").select(None, [("id", "=", 1)])[0], [(1, secret)])
        self.assertEqual(db.objects.get(oid), secret.encode() * 10)
        db.close()
        db = Database(self.dir, codec=StreamCipherCodec("wrong"))
        try:
            data = db.objects.get(oid)                 # garbage page pointers -> usually an error
        except Exception:
            data = None
        self.assertNotEqual(data, secret.encode() * 10)

if __name__ == "__main__":
    unittest.main()
