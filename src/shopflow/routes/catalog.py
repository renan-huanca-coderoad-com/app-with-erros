from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import Product

router = APIRouter(prefix="/products", tags=["catalog"])


class ProductIn(BaseModel):
    sku: str
    name: str
    price_cents: int
    stock: int


def _serialize(product: Product) -> dict:
    return {
        "id": product.id,
        "sku": product.sku,
        "name": product.name,
        "price_cents": product.price_cents,
        "stock": product.stock,
    }


@router.get("")
def list_products(session: Session = Depends(get_session)):
    products = session.scalars(select(Product).order_by(Product.id)).all()
    return [_serialize(p) for p in products]


@router.get("/{product_id}")
def get_product(product_id: int, session: Session = Depends(get_session)):
    product = session.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="product not found")
    detail = _serialize(product)
    # purchasing keeps asking how many units are in a pack — pull it out
    # of the product name so the detail page can show it on its own line
    detail["pack_size"] = product.name.split("(")[1].rstrip(")")
    return detail


@router.post("", status_code=201)
def add_product(payload: ProductIn, session: Session = Depends(get_session)):
    """Add a product to the catalog. Also used by the warehouse restock sync."""
    product = Product(
        sku=payload.sku,
        name=payload.name,
        price_cents=payload.price_cents,
        stock=payload.stock,
    )
    session.add(product)
    session.commit()
    return _serialize(product)


@router.delete("/{product_id}", status_code=204)
def discontinue_product(product_id: int, session: Session = Depends(get_session)):
    """Remove a discontinued product from the catalog."""
    product = session.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="product not found")
    session.delete(product)
    session.commit()
