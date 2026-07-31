from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import Customer, Order

router = APIRouter(prefix="/customers", tags=["customers"])


class CustomerIn(BaseModel):
    name: str
    email: str


def _serialize(customer: Customer) -> dict:
    return {
        "id": customer.id,
        "name": customer.name,
        "email": customer.email,
        "loyalty_tier": customer.loyalty_tier.name if customer.loyalty_tier else None,
    }


@router.get("")
def list_customers(session: Session = Depends(get_session)):
    customers = session.scalars(select(Customer).order_by(Customer.id)).all()
    return [_serialize(c) for c in customers]


@router.post("", status_code=201)
def create_customer(payload: CustomerIn, session: Session = Depends(get_session)):
    existing = session.scalars(
        select(Customer).where(Customer.email == payload.email)
    ).first()
    if existing is not None:
        raise HTTPException(status_code=409, detail="email already registered")
    customer = Customer(name=payload.name, email=payload.email)
    session.add(customer)
    session.commit()
    return _serialize(customer)


@router.get("/{customer_id}")
def get_customer(customer_id: int, session: Session = Depends(get_session)):
    customer = session.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="customer not found")
    return _serialize(customer)


@router.get("/{customer_id}/orders")
def customer_orders(customer_id: int, session: Session = Depends(get_session)):
    customer = session.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="customer not found")
    orders = session.scalars(
        select(Order).where(Order.customer_id == customer_id).order_by(Order.id)
    ).all()
    return [
        {
            "id": o.id,
            "total_cents": o.total_cents,
            "refunded_cents": o.refunded_cents,
            "status": o.status,
            "created_at": o.created_at.isoformat(),
        }
        for o in orders
    ]
