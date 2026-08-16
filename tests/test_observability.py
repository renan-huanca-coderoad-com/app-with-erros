"""Tests for request correlation IDs and error fingerprinting.

These deliberately avoid creating orders or customers: the smoke suite
shares one imported app instance with this module, so anything written
here would shift the state its assertions rely on.
"""

import json
import os
import tempfile
import time
import unittest
import uuid

_TMPDIR = tempfile.mkdtemp(prefix="shopflow-obs-")
os.environ.setdefault("SHOPFLOW_DB", os.path.join(_TMPDIR, "test.db"))
os.environ.setdefault("SHOPFLOW_ERROR_LOG", os.path.join(_TMPDIR, "errors.log"))

from fastapi.testclient import TestClient  # noqa: E402

import shopflow.app as app_module  # noqa: E402
from shopflow.db import SessionLocal  # noqa: E402
from shopflow.observability import (  # noqa: E402
    app_frame,
    fingerprint,
    normalize_message,
    take_request_id,
    uuid7,
)
from shopflow.routes.reports import sales_summary  # noqa: E402

# A real SQLAlchemy IntegrityError message: one bug, two occurrences,
# differing only in the bound parameters.
DB_ERROR_A = (
    "(sqlite3.IntegrityError) CHECK constraint failed: stock_non_negative\n"
    "[SQL: UPDATE products SET stock=? WHERE products.id = ?]\n"
    "[parameters: (-3, 5)]\n"
    "(Background on this error at: https://sqlalche.me/e/20/gkpj)"
)
DB_ERROR_B = (
    "(sqlite3.IntegrityError) CHECK constraint failed: stock_non_negative\n"
    "[SQL: UPDATE products SET stock=? WHERE products.id = ?]\n"
    "[parameters: (-17, 11)]\n"
    "(Background on this error at: https://sqlalche.me/e/20/gkpj)"
)


def _caught(exc_type, message):
    """An exception carrying a real traceback, raised from this module."""
    try:
        raise exc_type(message)
    except exc_type as exc:
        return exc


class Uuid7Test(unittest.TestCase):
    def test_is_a_version_7_uuid(self):
        value = uuid.UUID(uuid7())
        self.assertEqual(value.version, 7)
        self.assertEqual(value.variant, uuid.RFC_4122)

    def test_leading_bits_are_the_current_time(self):
        milliseconds = uuid.UUID(uuid7()).int >> 80
        self.assertAlmostEqual(milliseconds / 1000, time.time(), delta=5)

    def test_ids_sort_chronologically(self):
        early = uuid7()
        time.sleep(0.01)
        late = uuid7()
        self.assertLess(early, late)

    def test_ids_are_unique(self):
        self.assertEqual(len({uuid7() for _ in range(1000)}), 1000)


class TakeRequestIdTest(unittest.TestCase):
    def test_honors_a_well_formed_inbound_id(self):
        self.assertEqual(take_request_id("abc-123_DEF.456"), "abc-123_DEF.456")

    def test_mints_one_when_absent(self):
        self.assertEqual(uuid.UUID(take_request_id(None)).version, 7)

    def test_rejects_ids_that_could_forge_a_log_line_or_header(self):
        for hostile in ('x\n{"level": "INFO"}', "a b", "x\r\nX-Admin: 1", "é" * 10):
            with self.subTest(hostile=hostile):
                self.assertEqual(uuid.UUID(take_request_id(hostile)).version, 7)

    def test_rejects_ids_that_are_too_short_or_too_long(self):
        for hostile in ("", "short", "x" * 129):
            with self.subTest(hostile=hostile):
                self.assertEqual(uuid.UUID(take_request_id(hostile)).version, 7)


class NormalizeMessageTest(unittest.TestCase):
    def test_strips_sql_parameters_and_docs_link(self):
        self.assertEqual(
            normalize_message(DB_ERROR_A),
            "(sqliteN.IntegrityError) CHECK constraint failed: stock_non_negative",
        )

    def test_strips_memory_addresses(self):
        self.assertEqual(
            normalize_message("<Product object at 0x7f3ab2c10d90> is detached"),
            "<Product object at ADDR> is detached",
        )

    def test_leaves_the_identifying_part_of_a_message_alone(self):
        message = "'NoneType' object has no attribute 'discount_pct'"
        self.assertEqual(normalize_message(message), message)


class FingerprintTest(unittest.TestCase):
    def test_groups_occurrences_of_one_bug(self):
        self.assertEqual(
            fingerprint(_caught(ValueError, DB_ERROR_A)),
            fingerprint(_caught(ValueError, DB_ERROR_B)),
        )

    def test_separates_different_constraints(self):
        other = DB_ERROR_A.replace("stock_non_negative", "refund_within_total")
        self.assertNotEqual(
            fingerprint(_caught(ValueError, DB_ERROR_A)),
            fingerprint(_caught(ValueError, other)),
        )

    def test_separates_different_exception_types(self):
        self.assertNotEqual(
            fingerprint(_caught(ValueError, "boom")),
            fingerprint(_caught(TypeError, "boom")),
        )


class AppFrameTest(unittest.TestCase):
    def test_picks_the_handler_over_framework_plumbing(self):
        # caught by hand rather than with assertRaises, which discards the
        # traceback (``with_traceback(None)``) that this is all about
        with SessionLocal() as session:
            try:
                # a range with no orders in it — planted bug 7
                sales_summary(start="2020-01-01", end="2020-01-02", session=session)
            except ZeroDivisionError as exc:
                caught = exc
            else:
                self.fail("expected sales_summary to raise ZeroDivisionError")
        self.assertEqual(app_frame(caught), "routes/reports.py:sales_summary")

    def test_reports_when_no_frame_belongs_to_the_app(self):
        self.assertEqual(app_frame(_caught(ValueError, "boom")), "<no in-app frame>")


class RequestIdMiddlewareTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app_module.app, raise_server_exceptions=False)

    def test_successful_responses_carry_a_request_id(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(uuid.UUID(response.headers["x-request-id"]).version, 7)

    def test_inbound_request_id_is_propagated(self):
        response = self.client.get("/health", headers={"X-Request-ID": "trace-abc-123"})
        self.assertEqual(response.headers["x-request-id"], "trace-abc-123")

    def test_each_request_gets_a_distinct_id(self):
        first = self.client.get("/health").headers["x-request-id"]
        second = self.client.get("/health").headers["x-request-id"]
        self.assertNotEqual(first, second)

    def test_failed_request_correlates_header_body_and_log(self):
        response = self.client.get(
            "/reports/sales-summary",
            params={"start": "2020-01-01", "end": "2020-01-02"},
            headers={"X-Request-ID": "trace-failure-1"},
        )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.headers["x-request-id"], "trace-failure-1")

        body = response.json()
        self.assertEqual(body["request_id"], "trace-failure-1")
        self.assertEqual(uuid.UUID(body["event_id"]).version, 7)
        # the client is told nothing about what actually broke
        self.assertEqual(body["detail"], "Internal Server Error")

        record = self._last_log_record()
        self.assertEqual(record["request_id"], "trace-failure-1")
        self.assertEqual(record["event_id"], body["event_id"])
        self.assertEqual(record["exception_type"], "ZeroDivisionError")

    def test_repeat_failures_share_a_fingerprint_but_not_an_event_id(self):
        params = {"start": "2020-02-01", "end": "2020-02-02"}
        self.client.get("/reports/sales-summary", params=params)
        first = self._last_log_record()
        self.client.get("/reports/sales-summary", params=params)
        second = self._last_log_record()

        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertNotEqual(first["event_id"], second["event_id"])
        self.assertNotEqual(first["request_id"], second["request_id"])

    def _last_log_record(self) -> dict:
        # read the path off the module: the smoke suite may have won the
        # race to import it and bound a different temp directory
        lines = app_module.ERROR_LOG.read_text().strip().splitlines()
        return json.loads(lines[-1])


if __name__ == "__main__":
    unittest.main()
