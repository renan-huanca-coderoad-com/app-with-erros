"""Happy-path smoke tests for the NorthStar Supplies API.

The planted bugs are intentional demo features, so they are NOT covered
here — this suite pins down the behavior that is supposed to work.
"""

import os
import tempfile
import unittest
from datetime import date, timedelta

_TMPDIR = tempfile.mkdtemp(prefix="shopflow-test-")
os.environ["SHOPFLOW_DB"] = os.path.join(_TMPDIR, "test.db")
os.environ["SHOPFLOW_LOG"] = os.path.join(_TMPDIR, "app.log")

from fastapi.testclient import TestClient  # noqa: E402

from shopflow.app import app  # noqa: E402


class SmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_health(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_seeded_products_listed(self):
        response = self.client.get("/products")
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(len(response.json()), 12)

    def test_create_and_fetch_customer(self):
        created = self.client.post(
            "/customers",
            json={"name": "Test Corp", "email": "test-corp@example.test"},
        )
        self.assertEqual(created.status_code, 201)
        customer_id = created.json()["id"]
        fetched = self.client.get(f"/customers/{customer_id}")
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.json()["email"], "test-corp@example.test")

    def test_duplicate_email_rejected(self):
        payload = {"name": "Dup Co", "email": "dup@example.test"}
        self.assertEqual(self.client.post("/customers", json=payload).status_code, 201)
        self.assertEqual(self.client.post("/customers", json=payload).status_code, 409)

    def test_checkout_applies_loyalty_discount(self):
        # customer 1 is gold (8%); product 1 costs 649
        response = self.client.post(
            "/orders",
            json={"customer_id": 1, "items": [{"product_id": 1, "quantity": 2}]},
        )
        self.assertEqual(response.status_code, 201)
        order = response.json()
        self.assertEqual(order["subtotal_cents"], 1298)
        self.assertEqual(order["discount_cents"], int(1298 * 0.08))
        self.assertEqual(
            order["total_cents"], order["subtotal_cents"] - order["discount_cents"]
        )

    def test_checkout_decrements_stock(self):
        before = self.client.get("/products/2").json()["stock"]
        self.client.post(
            "/orders",
            json={"customer_id": 1, "items": [{"product_id": 2, "quantity": 3}]},
        )
        after = self.client.get("/products/2").json()["stock"]
        self.assertEqual(after, before - 3)

    def test_coupon_with_minimum_met(self):
        # product 6 costs 41500 >= WELCOME10 minimum of 5000
        response = self.client.post(
            "/orders",
            json={
                "customer_id": 4,  # bronze, 2%
                "items": [{"product_id": 6, "quantity": 1}],
                "coupon_code": "WELCOME10",
            },
        )
        self.assertEqual(response.status_code, 201)
        expected = int(41500 * 0.10) + int(41500 * 0.02)
        self.assertEqual(response.json()["discount_cents"], expected)

    def test_unknown_coupon_rejected(self):
        response = self.client.post(
            "/orders",
            json={
                "customer_id": 1,
                "items": [{"product_id": 1, "quantity": 1}],
                "coupon_code": "SUMMER20",
            },
        )
        self.assertEqual(response.status_code, 404)

    def test_single_line_over_stock_rejected(self):
        response = self.client.post(
            "/orders",
            json={"customer_id": 1, "items": [{"product_id": 1, "quantity": 99999}]},
        )
        self.assertEqual(response.status_code, 409)

    def test_single_refund_succeeds(self):
        order = self.client.post(
            "/orders",
            json={"customer_id": 2, "items": [{"product_id": 7, "quantity": 1}]},
        ).json()
        response = self.client.post(
            f"/orders/{order['id']}/refunds",
            json={"amount_cents": order["total_cents"] // 2, "reason": "damaged"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["refunded_cents"], order["total_cents"] // 2
        )

    def test_sales_summary_with_orders(self):
        self.client.post(
            "/orders",
            json={"customer_id": 3, "items": [{"product_id": 8, "quantity": 2}]},
        )
        # orders are stamped in UTC; use a window wide enough for any local tz
        start = (date.today() - timedelta(days=1)).isoformat()
        end = (date.today() + timedelta(days=1)).isoformat()
        response = self.client.get(
            f"/reports/sales-summary?start={start}&end={end}"
        )
        self.assertEqual(response.status_code, 200)
        report = response.json()
        self.assertGreaterEqual(report["order_count"], 1)
        self.assertGreater(report["avg_order_cents"], 0)


if __name__ == "__main__":
    unittest.main()
