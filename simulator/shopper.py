"""A simulated NorthStar Supplies user.

Behaves like a normal mix of shoppers and back-office staff: browses the
catalog, signs up new customers, places orders, applies coupons, requests
refunds, restocks products and runs reports. It has no knowledge of any
bugs in the backend — when a request fails it shrugs, notes the failure
and moves on, just like a real user would.

Usage:
    uv run python simulator/shopper.py --ops 60 --seed 42
"""

import argparse
import random
import time

import httpx

COUPON_CODES = ["WELCOME10", "BULK15", "FLASH5", "SUMMER20"]

COMPANY_WORDS = [
    "Summit", "Harbor", "Pioneer", "Cascade", "Beacon", "Orchard",
    "Granite", "Willow", "Compass", "Lakeside", "Ridgeline", "Juniper",
]
COMPANY_KINDS = ["Consulting", "Logistics", "Studio", "Clinic", "Academy", "Labs"]

REPORT_RANGES = [
    ("2026-07-25", "2026-07-31"),  # this week: usually has orders
    ("2026-07-01", "2026-07-31"),  # this month
    ("2026-01-01", "2026-01-07"),  # quiet week far in the past
    ("2025-11-01", "2025-11-30"),  # before the shop even existed
]


class Shopper:
    def __init__(self, base_url: str, rng: random.Random):
        self.client = httpx.Client(base_url=base_url, timeout=10)
        self.rng = rng
        self.customer_ids: list[int] = []
        self.product_ids: list[int] = []
        self.known_skus: list[str] = []
        self.orders: list[dict] = []  # {"id": ..., "total_cents": ...}
        self.signup_counter = 0
        self.failures = 0
        self.ops = 0

    # -- plumbing ---------------------------------------------------------

    def request(self, method: str, path: str, **kwargs) -> httpx.Response | None:
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            print(f"  !! network problem talking to the shop: {exc}")
            self.failures += 1
            return None
        if response.status_code >= 500:
            request_id = response.headers.get("x-request-id", "?")
            print(f"  !! hm, the site errored out ({response.status_code}, "
                  f"request_id={request_id}) — oh well, moving on")
            self.failures += 1
        elif response.status_code >= 400:
            detail = response.json().get("detail", "")
            print(f"  -- request rejected ({response.status_code}): {detail}")
        return response

    def bootstrap(self) -> None:
        products = self.client.get("/products").json()
        self.product_ids = [p["id"] for p in products]
        self.known_skus = [p["sku"] for p in products]
        customers = self.client.get("/customers").json()
        self.customer_ids = [c["id"] for c in customers]
        print(f"discovered {len(products)} products, {len(customers)} customers")

    # -- user behaviors ---------------------------------------------------

    def browse(self) -> None:
        product_id = self.rng.choice(self.product_ids)
        print(f"browsing the catalog, looking at product {product_id}")
        self.request("GET", "/products")
        self.request("GET", f"/products/{product_id}")

    def signup(self) -> None:
        self.signup_counter += 1
        name = (f"{self.rng.choice(COMPANY_WORDS)} "
                f"{self.rng.choice(COMPANY_KINDS)}")
        email = f"contact{self.signup_counter}.{self.rng.randint(100, 999)}@{name.split()[0].lower()}.example"
        print(f"new customer signing up: {name}")
        response = self.request("POST", "/customers", json={"name": name, "email": email})
        if response is not None and response.status_code == 201:
            self.customer_ids.append(response.json()["id"])

    def build_cart(self) -> list[dict]:
        # sometimes a purchasing manager checks what's left of a product and
        # splits roughly half of it between two departments, one cart line each
        if self.rng.random() < 0.2:
            product_id = self.rng.choice(self.product_ids)
            response = self.request("GET", f"/products/{product_id}")
            if response is not None and response.status_code == 200:
                stock = response.json()["stock"]
                if stock >= 4:
                    per_dept = (stock + 1) // 2 + self.rng.randint(0, 2)
                    return [
                        {"product_id": product_id, "quantity": per_dept},
                        {"product_id": product_id, "quantity": per_dept},
                    ]
        lines = []
        for _ in range(self.rng.randint(1, 3)):
            lines.append({
                "product_id": self.rng.choice(self.product_ids),
                "quantity": self.rng.randint(1, 6),
            })
        # occasionally the shopper adds the same item to the cart again
        # later in the session, in bulk
        if self.rng.random() < 0.35:
            repeat = dict(self.rng.choice(lines))
            repeat["quantity"] = self.rng.randint(5, 15)
            lines.append(repeat)
        return lines

    def place_order(self, with_coupon: bool = False) -> None:
        customer_id = self.rng.choice(self.customer_ids)
        payload = {"customer_id": customer_id, "items": self.build_cart()}
        label = f"customer {customer_id} checking out {len(payload['items'])} line(s)"
        if with_coupon:
            payload["coupon_code"] = self.rng.choice(COUPON_CODES)
            label += f" with coupon {payload['coupon_code']}"
        print(label)
        response = self.request("POST", "/orders", json=payload)
        if response is not None and response.status_code == 201:
            order = response.json()
            self.orders.append({"id": order["id"], "total_cents": order["total_cents"]})

    def refund(self) -> None:
        if not self.orders:
            print("wanted a refund but has no orders yet")
            return
        order = self.rng.choice(self.orders)
        amount = int(order["total_cents"] * self.rng.uniform(0.3, 0.9))
        if amount < 1:
            return
        print(f"requesting a {amount}c refund on order {order['id']}")
        self.request("POST", f"/orders/{order['id']}/refunds",
                     json={"amount_cents": amount,
                           "reason": self.rng.choice(["damaged", "wrong item", "changed mind"])})

    def restock(self) -> None:
        if self.rng.random() < 0.5:
            sku = self.rng.choice(self.known_skus)
            name = "Restocked item"
        else:
            sku = f"NEW-{self.rng.randint(1000, 9999)}"
            name = f"Seasonal item {sku}"
            self.known_skus.append(sku)
        print(f"warehouse pushing a restock for {sku}")
        response = self.request("POST", "/products", json={
            "sku": sku,
            "name": name,
            "price_cents": self.rng.randint(500, 30000),
            "stock": self.rng.randint(10, 100),
        })
        if response is not None and response.status_code == 201:
            self.product_ids.append(response.json()["id"])

    def discontinue(self) -> None:
        product_id = self.rng.choice(self.product_ids)
        print(f"back office discontinuing product {product_id}")
        response = self.request("DELETE", f"/products/{product_id}")
        if response is not None and response.status_code == 204:
            self.product_ids.remove(product_id)

    def report(self) -> None:
        start, end = self.rng.choice(REPORT_RANGES)
        print(f"manager pulling the sales summary for {start}..{end}")
        self.request("GET", "/reports/sales-summary",
                     params={"start": start, "end": end})

    # -- main loop --------------------------------------------------------

    ACTIONS = [
        ("browse", 18),
        ("signup", 8),
        ("place_order", 26),
        ("order_with_coupon", 12),
        ("refund", 12),
        ("restock", 10),
        ("discontinue", 5),
        ("report", 9),
    ]

    def run(self, ops: int, delay: float) -> None:
        self.bootstrap()
        names = [name for name, _ in self.ACTIONS]
        weights = [weight for _, weight in self.ACTIONS]
        for i in range(ops):
            action = self.rng.choices(names, weights=weights)[0]
            print(f"[{i + 1:03d}/{ops}] ", end="")
            if action == "order_with_coupon":
                self.place_order(with_coupon=True)
            else:
                getattr(self, action)()
            self.ops += 1
            if delay:
                time.sleep(delay)
        print(f"\ndone: {self.ops} operations, "
              f"{self.failures} of them hit server errors")


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulated NorthStar Supplies user")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--ops", type=int, default=60)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--delay", type=float, default=0.1,
                        help="pause between operations, seconds")
    args = parser.parse_args()

    shopper = Shopper(args.base_url, random.Random(args.seed))
    shopper.run(args.ops, args.delay)


if __name__ == "__main__":
    main()
