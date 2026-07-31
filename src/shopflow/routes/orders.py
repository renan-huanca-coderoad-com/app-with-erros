from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import Coupon, Customer, Order, OrderItem, Product

router = APIRouter(prefix="/orders", tags=["orders"])


class OrderLine(BaseModel):
    product_id: int
    quantity: int


class OrderIn(BaseModel):
    customer_id: int
    items: list[OrderLine]
    coupon_code: str | None = None


class RefundIn(BaseModel):
    amount_cents: int
    reason: str = ""


def _serialize(order: Order) -> dict:
    return {
        "id": order.id,
        "customer_id": order.customer_id,
        "subtotal_cents": order.subtotal_cents,
        "discount_cents": order.discount_cents,
        "total_cents": order.total_cents,
        "refunded_cents": order.refunded_cents,
        "status": order.status,
        "created_at": order.created_at.isoformat(),
        "items": [
            {
                "product_id": item.product_id,
                "quantity": item.quantity,
                "unit_price_cents": item.unit_price_cents,
            }
            for item in order.items
        ],
    }


@router.post("", status_code=201)
def create_order(payload: OrderIn, session: Session = Depends(get_session)):
    customer = session.get(Customer, payload.customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="customer not found")
    if not payload.items:
        raise HTTPException(status_code=422, detail="order has no items")

    subtotal = 0
    for line in payload.items:
        product = session.get(Product, line.product_id)
        if product is None:
            raise HTTPException(
                status_code=404, detail=f"product {line.product_id} not found"
            )
        if line.quantity < 1:
            raise HTTPException(status_code=422, detail="quantity must be positive")
        if line.quantity > product.stock:
            raise HTTPException(
                status_code=409,
                detail=f"insufficient stock for {product.sku}",
            )
        subtotal += product.price_cents * line.quantity

    discount = 0
    if payload.coupon_code:
        coupon = session.scalars(
            select(Coupon).where(Coupon.code == payload.coupon_code)
        ).first()
        if coupon is None:
            raise HTTPException(status_code=404, detail="unknown coupon code")
        if subtotal >= coupon.min_order_cents:
            discount += int(subtotal * coupon.percent_off / 100)

    loyalty_pct = customer.loyalty_tier.discount_pct
    discount += int(subtotal * loyalty_pct / 100)

    order = Order(
        customer_id=customer.id,
        subtotal_cents=subtotal,
        discount_cents=discount,
        total_cents=subtotal - discount,
    )
    session.add(order)
    session.flush()

    for line in payload.items:
        product = session.get(Product, line.product_id)
        product.stock -= line.quantity
        session.add(
            OrderItem(
                order_id=order.id,
                product_id=product.id,
                quantity=line.quantity,
                unit_price_cents=product.price_cents,
            )
        )

    session.commit()
    return _serialize(order)


@router.get("/{order_id}")
def get_order(order_id: int, session: Session = Depends(get_session)):
    order = session.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return _serialize(order)


@router.post("/{order_id}/refunds")
def refund_order(
    order_id: int, payload: RefundIn, session: Session = Depends(get_session)
):
    order = session.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    if payload.amount_cents < 1:
        raise HTTPException(status_code=422, detail="refund amount must be positive")
    if payload.amount_cents > order.total_cents:
        raise HTTPException(
            status_code=400, detail="refund exceeds order total"
        )

    order.refunded_cents += payload.amount_cents
    if order.refunded_cents == order.total_cents:
        order.status = "refunded"
    session.commit()
    return _serialize(order)
