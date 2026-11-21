"""Retail Orders demo workload for M1 + M2 (domain: customers, products, orders, order_items, inventory)."""
import argparse, os, random, shutil, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from Pdb import Database, StreamCipherCodec

CATS = ["electronics", "books", "toys", "clothing", "garden", "sports", "grocery", "beauty"]
REGIONS = ["north", "south", "east", "west"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--products", type=int, default=5000)
    ap.add_argument("--customers", type=int, default=2000)
    ap.add_argument("--orders", type=int, default=10000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--dir", default=None)
    ap.add_argument("--encrypt", action="store_true", help="enable the demo encryption codec")
    a = ap.parse_args()
    path = a.dir or tempfile.mkdtemp(prefix="retail_")
    shutil.rmtree(path, ignore_errors=True)
    rng = random.Random(a.seed)
    codec = StreamCipherCodec("demo-key") if a.encrypt else None
    db = Database(path, buffer_frames=256, policy="lru", codec=codec)

    # schema: dimension tables row-oriented, order_items columnar
    db.execute("CREATE TABLE customers (customer_id INT, name TEXT(40), region TEXT(12), email TEXT(48)) USING ROW")
    db.execute("CREATE TABLE products (product_id INT, name TEXT(40), category TEXT(16), price FLOAT, image_oid TEXT(16)) USING ROW")
    db.execute("CREATE TABLE inventory (product_id INT, warehouse TEXT(12), stock INT) USING ROW")
    db.execute("CREATE TABLE orders (order_id INT, customer_id INT, order_date TEXT(10), total FLOAT) USING ROW")
    db.execute("CREATE TABLE order_items (order_id INT, product_id INT, quantity INT, unit_price FLOAT) USING COLUMN")

    # load synthetic data; product images live in the object store
    t = db.table("customers")
    t.insert_many([(i, f"customer-{i}", rng.choice(REGIONS), f"c{i}@example.com") for i in range(a.customers)])
    t = db.table("products")
    prices = {}
    for i in range(a.products):
        prices[i] = round(rng.uniform(2, 900), 2)
        oid = db.objects.put(bytes(rng.getrandbits(8) for _ in range(rng.choice([300, 3000, 9000]))))   # fake image bytes
        t.insert((i, f"product-{i}", rng.choice(CATS), prices[i], oid))
    db.table("inventory").insert_many([(i, rng.choice(["W1", "W2"]), rng.randrange(500)) for i in range(a.products)])
    orders, items = db.table("orders"), db.table("order_items")
    for o in range(a.orders):
        n = rng.randint(1, 4)
        lines = [(o, p, rng.randint(1, 5), prices[p]) for p in rng.sample(range(a.products), n)]
        items.insert_many(lines)
        orders.insert((o, rng.randrange(a.customers), f"2026-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
                       round(sum(q * pr for _, _, q, pr in lines), 2)))

    # indexes (AUTO decides from the workload observed so far: loading was write-heavy)
    for stmt in ["CREATE INDEX ix_prod_id ON products (product_id) USING BTREE",
                 "CREATE INDEX ix_prod_cat ON products (category) USING BTREE",
                 "CREATE INDEX ix_cust_id ON customers (customer_id) USING BTREE",
                 "CREATE INDEX ix_ord_id ON orders (order_id) USING AUTO",
                 "CREATE INDEX ix_ord_date ON orders (order_date) USING BTREE"]:
        print(db.execute(stmt).message)

    queries = ["SELECT name, price FROM products WHERE product_id = 42",
               "SELECT product_id, price FROM products WHERE product_id BETWEEN 100 AND 105",
               "SELECT COUNT(*) FROM products WHERE category = 'books'",
               "SELECT order_id, total FROM orders WHERE order_date BETWEEN '2026-03-01' AND '2026-03-03' LIMIT 5",
               "SELECT quantity, unit_price FROM order_items WHERE order_id = 7",
               "EXPLAIN SELECT * FROM customers WHERE region = 'north'"]
    for q in queries:
        print("\nsql>", q)
        print(db.execute(q))

    print("\nsql> UPDATE products SET price = 19.99 WHERE product_id = 42")
    print(db.execute("UPDATE products SET price = 19.99 WHERE product_id = 42").message)
    print(db.execute("SELECT product_id, price FROM products WHERE product_id = 42"))

    oid = db.execute("SELECT image_oid FROM products WHERE product_id = 42").rows[0][0]
    print(f"\nproduct 42 image object {oid}: {len(db.objects.get(oid))} bytes read back from the object store")
    print("\nbuffer pool stats:", db.pool.stats())

    db.close()
    db = Database(path, buffer_frames=256, codec=codec)           # restart -> everything persisted
    print("\nafter restart:", db.execute("SELECT name, price FROM products WHERE product_id = 42").rows)
    db.close()
    print("database directory:", path)


if __name__ == "__main__":
    main()
