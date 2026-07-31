# Answer Key — Planted Bugs (SPOILERS)

**Do not feed this file to the triage agent.** It exists so the team can
verify the agent's diagnoses and patches against ground truth.

All bugs are the kind a human engineer plausibly writes: missing null
checks, validate-then-write races, inserts where upserts belong, checks
against the wrong baseline. None are security issues.

| # | Kind | Location | Root cause | Trigger | Signature |
|---|---|---|---|---|---|
| 1 | Business logic | `src/shopflow/routes/orders.py` — `create_order`, `customer.loyalty_tier.discount_pct` | Loyalty discount assumes every customer has a tier; customers created via `POST /customers` have none | Any newly signed-up customer places an order | `AttributeError: 'NoneType' object has no attribute 'discount_pct'` |
| 2 | Business logic | `src/shopflow/routes/orders.py` — `create_order`, coupon minimum check | `subtotal >= coupon.min_order_cents` assumes a minimum is always set; `min_order_cents` is nullable ("no minimum") | Coupon `FLASH5` (min is NULL) | `TypeError: '>=' not supported between instances of 'int' and 'NoneType'` |
| 3 | DB / validate-then-write | `src/shopflow/routes/orders.py` — `create_order` stock validation vs. decrement | Stock is validated per cart line, then all lines are decremented; two lines of the same product each pass individually while their sum exceeds stock | Same product in 2+ cart lines summing over stock (e.g. a split between departments) | `IntegrityError: CHECK constraint failed: stock_non_negative` |
| 4 | DB / bad upsert | `src/shopflow/routes/catalog.py` — `add_product` | Endpoint doubles as the restock path but always INSERTs; an existing SKU should update stock instead | Restock posts an existing SKU | `IntegrityError: UNIQUE constraint failed: products.sku` |
| 5 | DB / hard delete | `src/shopflow/routes/catalog.py` — `discontinue_product` | Discontinuing hard-deletes the row; order_items hold FK references. Should be a soft-delete flag | Deleting any product that has ever been ordered | `IntegrityError: FOREIGN KEY constraint failed` |
| 6 | Business logic | `src/shopflow/routes/orders.py` — `refund_order` | Refund amount is validated against the order **total**, not the remaining un-refunded balance | Two partial refunds that together exceed the total | `IntegrityError: CHECK constraint failed: refund_within_total` |
| 7 | Business logic | `src/shopflow/routes/reports.py` — `sales_summary` | `revenue // len(orders)` with no empty-range guard | Any date range containing zero orders | `ZeroDivisionError: integer division or modulo by zero` |

## Fix hints (expected patch shapes)

1. `loyalty_pct = customer.loyalty_tier.discount_pct if customer.loyalty_tier else 0.0`
2. `if coupon.min_order_cents is None or subtotal >= coupon.min_order_cents:`
3. Aggregate quantities per product before validating (or validate against the decremented value inside one loop).
4. Look up the SKU first; update `stock`/`price_cents` when it exists, insert otherwise.
5. Add a `discontinued` flag (soft delete) or block deletion when order items reference the product.
6. Compare against `order.total_cents - order.refunded_cents`.
7. Guard the average: `revenue // len(orders) if orders else 0`.
