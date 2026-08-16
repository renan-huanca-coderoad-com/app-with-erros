"""Keep the simulated world stationary between traffic bursts.

Without this the corpus is quietly wrong. The simulator consumes the
world as it runs: stock drains, tier-less customers pile up, the catalog
fills with paren-free products. Left alone across a multi-day baseline
that turns every ambient bug into a *trend* — bug 1 climbing from 0% to
100% reads as a second, larger regression, and the planted one lands at
90% instead of the 58% the seed data implies.

So between bursts the world is groomed back toward its starting shape.
Every operation here is something a real operations team genuinely does
on a schedule, which is the reason it is defensible rather than a cheat.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

# The catalog as seeded, from db.py's _seed(). Five names carry a
# parenthesised pack size and seven do not, which is what pins the
# planted regression's failure rate.
SEEDED_PRODUCTS = [
    ("PAP-A4-500", "Copy Paper A4 (500 sheets)", 649, 180),
    ("PEN-BLK-12", "Ballpoint Pens Black (12-pack)", 489, 140),
    ("TON-HP-83A", "Toner Cartridge HP 83A", 7899, 35),
    ("CHR-ERG-01", "Ergonomic Office Chair", 18999, 12),
    ("DSK-STD-120", "Standing Desk 120cm", 32900, 8),
    ("MON-27-4K", "27in 4K Monitor", 41500, 15),
    ("KBD-MEC-TKL", "Mechanical Keyboard TKL", 8990, 40),
    ("LBL-THR-50", "Thermal Label Rolls (50)", 2350, 90),
    ("BOX-MED-25", "Shipping Boxes Medium (25)", 3199, 60),
    ("TAP-PCK-6", "Packing Tape (6 rolls)", 1249, 110),
    ("STP-HDY-01", "Heavy Duty Stapler", 2799, 25),
    ("WHT-BRD-90", "Whiteboard 90x60cm", 5450, 18),
]

# Share of customers left without a loyalty tier after each CRM sync.
# Expressed as a target share rather than a per-pass probability: a
# probability compounds, so 85%-per-pass over a dozen passes drains the
# tier-less population to nothing and bug 1 quietly stops firing halfway
# through the baseline. A target holds the world stationary.
TARGET_TIER_LESS_SHARE = 0.15

_TIER_COUNT = 3


@dataclass
class WorldStats:
    restocked: int
    reinstated: int
    pruned: int
    tiers_assigned: int


def _connect(db_path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(db_path))
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def prepare_world(db_path, *, reference: datetime, rng) -> int:
    """Backfill order history so the sales report is not a 100% siren.

    All four date ranges the simulator asks for sit in the past, while
    live orders are stamped now — so every report hits the empty-range
    bug and that endpoint fails on every single call. A 100%-failure
    endpoint drowns out the planted regression instead of being ambient
    noise. Backfilling real history leaves three of the four ranges
    resolving and the fourth ("before the shop existed") still empty, so
    the bug fires at a steady ~25% on both sides of the deploy.
    """
    connection = _connect(db_path)
    try:
        customer_ids = [
            row[0] for row in connection.execute("SELECT id FROM customers")
        ]
        product_rows = list(
            connection.execute("SELECT id, price_cents FROM products")
        )
        if not customer_ids or not product_rows:
            raise RuntimeError("world must be seeded before backfilling history")

        windows = [
            (datetime(2026, 7, 1), datetime(2026, 7, 31), 170),
            (datetime(2026, 1, 1), datetime(2026, 1, 7), 30),
        ]
        created = 0
        for start, end, count in windows:
            span = int((end - start).total_seconds())
            for _ in range(count):
                stamped = start + timedelta(seconds=rng.randrange(span))
                product_id, price = rng.choice(product_rows)
                quantity = rng.randint(1, 5)
                subtotal = price * quantity
                discount = int(subtotal * rng.choice([0.0, 0.02, 0.05, 0.08]))
                cursor = connection.execute(
                    "INSERT INTO orders (customer_id, subtotal_cents, "
                    "discount_cents, total_cents, refunded_cents, status, "
                    "created_at) VALUES (?, ?, ?, ?, 0, 'paid', ?)",
                    (
                        rng.choice(customer_ids),
                        subtotal,
                        discount,
                        subtotal - discount,
                        stamped.isoformat(sep=" "),
                    ),
                )
                connection.execute(
                    "INSERT INTO order_items (order_id, product_id, quantity, "
                    "unit_price_cents) VALUES (?, ?, ?, ?)",
                    (cursor.lastrowid, product_id, quantity, price),
                )
                created += 1
        connection.commit()
        return created
    finally:
        connection.close()


def groom_world(db_path, *, rng) -> WorldStats:
    """Restore the world toward its seeded shape. Never touches orders."""
    connection = _connect(db_path)
    try:
        stats = WorldStats(0, 0, 0, 0)

        # 1. a warehouse receiving run: top the seeded lines back up
        for sku, _name, _price, stock in SEEDED_PRODUCTS:
            changed = connection.execute(
                "UPDATE products SET stock = ? WHERE sku = ? AND stock < ?",
                (stock, sku, stock),
            ).rowcount
            stats.restocked += changed

        # 2. anything the back office discontinued comes back into the range
        existing = {
            row[0] for row in connection.execute("SELECT sku FROM products")
        }
        for sku, name, price, stock in SEEDED_PRODUCTS:
            if sku not in existing:
                connection.execute(
                    "INSERT INTO products (sku, name, price_cents, stock) "
                    "VALUES (?, ?, ?, ?)",
                    (sku, name, price, stock),
                )
                stats.reinstated += 1

        # 3. retire simulator-created lines nobody ever ordered, so the
        #    catalog's paren-free share stops drifting upward
        stats.pruned = connection.execute(
            "DELETE FROM products WHERE sku NOT IN (%s) AND id NOT IN "
            "(SELECT DISTINCT product_id FROM order_items)"
            % ",".join("?" * len(SEEDED_PRODUCTS)),
            [sku for sku, *_ in SEEDED_PRODUCTS],
        ).rowcount

        # 4. a CRM sync works through the backlog of new signups, leaving a
        #    steady fraction still unclassified — the backlog a real sync
        #    never quite clears, and what keeps bug 1 firing at a constant
        #    rate instead of fading out across the baseline
        tier_less = [
            row[0]
            for row in connection.execute(
                "SELECT id FROM customers WHERE loyalty_tier_id IS NULL"
            )
        ]
        total = connection.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
        keep = int(total * TARGET_TIER_LESS_SHARE)
        excess = max(0, len(tier_less) - keep)
        for customer_id in rng.sample(tier_less, excess):
            connection.execute(
                "UPDATE customers SET loyalty_tier_id = ? WHERE id = ?",
                (rng.randint(1, _TIER_COUNT), customer_id),
            )
            stats.tiers_assigned += 1

        connection.commit()

        remaining = connection.execute(
            "SELECT COUNT(*) FROM products"
        ).fetchone()[0]
        if remaining == 0:
            # db.py's _seed() returns early only when a product exists; an
            # empty table would make the next boot re-seed and collide
            raise RuntimeError("grooming emptied the catalog")
        return stats
    finally:
        connection.close()


def world_shape(db_path) -> dict:
    """A snapshot used to show that grooming actually held the line."""
    connection = _connect(db_path)
    try:
        products = list(connection.execute("SELECT name FROM products"))
        customers, tier_less = connection.execute(
            "SELECT COUNT(*), COUNT(*) FILTER (WHERE loyalty_tier_id IS NULL) "
            "FROM customers"
        ).fetchone()
        paren_free = sum(1 for (name,) in products if "(" not in name)
        return {
            "products": len(products),
            "paren_free_share": paren_free / len(products) if products else 0.0,
            "customers": customers,
            "tier_less_share": tier_less / customers if customers else 0.0,
            "stock_total": connection.execute(
                "SELECT COALESCE(SUM(stock), 0) FROM products"
            ).fetchone()[0],
        }
    finally:
        connection.close()
