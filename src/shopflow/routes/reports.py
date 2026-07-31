from datetime import date, datetime, time, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import Order

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("/sales-summary")
def sales_summary(
    start: str, end: str, session: Session = Depends(get_session)
):
    """Revenue summary for a date range (inclusive, YYYY-MM-DD)."""
    try:
        start_day = date.fromisoformat(start)
        end_day = date.fromisoformat(end)
    except ValueError:
        raise HTTPException(status_code=422, detail="dates must be YYYY-MM-DD")
    if end_day < start_day:
        raise HTTPException(status_code=422, detail="end date before start date")

    start_dt = datetime.combine(start_day, time.min)
    end_dt = datetime.combine(end_day, time.min) + timedelta(days=1)

    orders = session.scalars(
        select(Order).where(Order.created_at >= start_dt, Order.created_at < end_dt)
    ).all()

    revenue = sum(o.total_cents for o in orders)
    refunded = sum(o.refunded_cents for o in orders)
    avg_order_cents = revenue // len(orders)

    return {
        "start": start,
        "end": end,
        "order_count": len(orders),
        "revenue_cents": revenue,
        "refunded_cents": refunded,
        "net_revenue_cents": revenue - refunded,
        "avg_order_cents": avg_order_cents,
    }
