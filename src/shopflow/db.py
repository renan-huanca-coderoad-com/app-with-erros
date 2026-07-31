import os

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from .models import Base, Coupon, Customer, LoyaltyTier, Product

DB_PATH = os.environ.get("SHOPFLOW_DB", "shopflow.db")

engine = create_engine(
    f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False}
)


@event.listens_for(engine, "connect")
def _enable_foreign_keys(dbapi_connection, _record):
    dbapi_connection.execute("PRAGMA foreign_keys=ON")


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def init_db() -> None:
    Base.metadata.create_all(engine)
    _seed()


def _seed() -> None:
    with SessionLocal() as session:
        if session.scalars(select(Product)).first() is not None:
            return

        bronze = LoyaltyTier(name="bronze", discount_pct=2.0)
        silver = LoyaltyTier(name="silver", discount_pct=5.0)
        gold = LoyaltyTier(name="gold", discount_pct=8.0)
        session.add_all([bronze, silver, gold])
        session.flush()

        products = [
            Product(sku="PAP-A4-500", name="Copy Paper A4 (500 sheets)", price_cents=649, stock=180),
            Product(sku="PEN-BLK-12", name="Ballpoint Pens Black (12-pack)", price_cents=489, stock=140),
            Product(sku="TON-HP-83A", name="Toner Cartridge HP 83A", price_cents=7899, stock=35),
            Product(sku="CHR-ERG-01", name="Ergonomic Office Chair", price_cents=18999, stock=12),
            Product(sku="DSK-STD-120", name="Standing Desk 120cm", price_cents=32900, stock=8),
            Product(sku="MON-27-4K", name="27in 4K Monitor", price_cents=41500, stock=15),
            Product(sku="KBD-MEC-TKL", name="Mechanical Keyboard TKL", price_cents=8990, stock=40),
            Product(sku="LBL-THR-50", name="Thermal Label Rolls (50)", price_cents=2350, stock=90),
            Product(sku="BOX-MED-25", name="Shipping Boxes Medium (25)", price_cents=3199, stock=60),
            Product(sku="TAP-PCK-6", name="Packing Tape (6 rolls)", price_cents=1249, stock=110),
            Product(sku="STP-HDY-01", name="Heavy Duty Stapler", price_cents=2799, stock=25),
            Product(sku="WHT-BRD-90", name="Whiteboard 90x60cm", price_cents=5450, stock=18),
        ]
        session.add_all(products)

        customers = [
            Customer(name="Acme Logistics", email="purchasing@acmelogistics.example", loyalty_tier_id=gold.id),
            Customer(name="Bluewater Consulting", email="office@bluewater.example", loyalty_tier_id=silver.id),
            Customer(name="Cedar Grove Dental", email="admin@cedargrove.example", loyalty_tier_id=silver.id),
            Customer(name="Delta Print Shop", email="orders@deltaprint.example", loyalty_tier_id=bronze.id),
            Customer(name="Evergreen Schools", email="supplies@evergreen.example", loyalty_tier_id=gold.id),
            Customer(name="Foundry Cowork", email="hello@foundrycowork.example", loyalty_tier_id=bronze.id),
        ]
        session.add_all(customers)

        coupons = [
            Coupon(code="WELCOME10", percent_off=10.0, min_order_cents=5000),
            Coupon(code="BULK15", percent_off=15.0, min_order_cents=25000),
            Coupon(code="FLASH5", percent_off=5.0, min_order_cents=None),
        ]
        session.add_all(coupons)

        session.commit()
