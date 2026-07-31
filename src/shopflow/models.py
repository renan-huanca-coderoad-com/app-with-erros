from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, ForeignKey
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class LoyaltyTier(Base):
    __tablename__ = "loyalty_tiers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
    discount_pct: Mapped[float]


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    email: Mapped[str] = mapped_column(unique=True)
    loyalty_tier_id: Mapped[int | None] = mapped_column(ForeignKey("loyalty_tiers.id"))

    loyalty_tier: Mapped[LoyaltyTier | None] = relationship()


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (CheckConstraint("stock >= 0", name="stock_non_negative"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(unique=True)
    name: Mapped[str]
    price_cents: Mapped[int]
    stock: Mapped[int]


class Coupon(Base):
    __tablename__ = "coupons"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(unique=True)
    percent_off: Mapped[float]
    min_order_cents: Mapped[int | None]


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("refunded_cents <= total_cents", name="refund_within_total"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    subtotal_cents: Mapped[int]
    discount_cents: Mapped[int] = mapped_column(default=0)
    total_cents: Mapped[int]
    refunded_cents: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(default="paid")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    customer: Mapped[Customer] = relationship()
    items: Mapped[list["OrderItem"]] = relationship(back_populates="order")


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    quantity: Mapped[int]
    unit_price_cents: Mapped[int]

    order: Mapped[Order] = relationship(back_populates="items")
    product: Mapped[Product] = relationship()
