"""Tests for request correlation IDs and error fingerprinting.

These deliberately avoid creating orders or customers: the smoke suite
shares one imported app instance with this module, so anything written
here would shift the state its assertions rely on.
"""

import json
import logging
import os
import tempfile
import time
import unittest
import uuid

_TMPDIR = tempfile.mkdtemp(prefix="shopflow-obs-")
os.environ.setdefault("SHOPFLOW_DB", os.path.join(_TMPDIR, "test.db"))
os.environ.setdefault("SHOPFLOW_LOG", os.path.join(_TMPDIR, "app.log"))

from fastapi.testclient import TestClient  # noqa: E402

import shopflow.app as app_module  # noqa: E402
import shopflow.observability as observability  # noqa: E402
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

    def test_successful_request_is_logged_too(self):
        self.client.get("/health", headers={"X-Request-ID": "trace-success-1"})
        record = self._last_log_record("shopflow.access")

        self.assertEqual(record["level"], "INFO")
        self.assertEqual(record["request_id"], "trace-success-1")
        self.assertEqual(record["message"], "GET /health 200")
        self.assertEqual(record["http.status_code"], 200)
        self.assertGreaterEqual(record["http.duration_ms"], 0)
        self.assertNotIn("traceback", record)
        # the body is only kept on the error line
        self.assertNotIn("http.body", record)

    def test_every_record_carries_deployment_context(self):
        self.client.get("/health")
        record = self._last_log_record("shopflow.access")
        for field in ("timestamp", "level", "logger", "message", "service",
                      "env", "version", "commit", "host", "pid"):
            self.assertIn(field, record)

    def test_commit_comes_from_the_environment(self):
        # the field a triage agent uses to jump from a log line to `git show`
        self.assertEqual(
            observability.COMMIT,
            os.environ.get("SHOPFLOW_COMMIT", "unknown"),
        )
        self.client.get("/health")
        self.assertEqual(
            self._last_log_record("shopflow.access")["commit"],
            observability.COMMIT,
        )

    def test_startup_announces_the_running_build(self):
        # a restart is otherwise invisible in the stream except as a new pid
        with TestClient(app_module.app):
            pass
        records = [r for r in self._records() if r["logger"] == "shopflow.lifecycle"]
        startup = [r for r in records if r.get("event") == "startup"][-1]
        shutdown = [r for r in records if r.get("event") == "shutdown"][-1]

        self.assertEqual(startup["level"], "INFO")
        self.assertIn(observability.COMMIT, startup["message"])
        self.assertIn(startup["version"], startup["message"])
        self.assertIn(observability.COMMIT, shutdown["message"])
        # no request produced these, so they carry the contextvar default
        self.assertEqual(startup["request_id"], "-")

    def test_client_rejection_is_logged_as_a_warning(self):
        response = self.client.get("/products/999999")
        self.assertEqual(response.status_code, 404)
        record = self._last_log_record("shopflow.access")
        self.assertEqual(record["level"], "WARNING")
        self.assertEqual(record["http.status_code"], 404)

    def test_failure_writes_both_an_error_and_an_access_line(self):
        self.client.get(
            "/reports/sales-summary",
            params={"start": "2020-03-01", "end": "2020-03-02"},
            headers={"X-Request-ID": "trace-pair-1"},
        )
        pair = [r for r in self._records() if r["request_id"] == "trace-pair-1"]
        self.assertEqual(len(pair), 2)

        error, access = pair
        self.assertEqual(error["logger"], "shopflow.error")
        self.assertEqual(error["exception_type"], "ZeroDivisionError")
        self.assertIn("traceback", error)
        self.assertIn("http.body", error)

        self.assertEqual(access["logger"], "shopflow.access")
        self.assertEqual(access["level"], "ERROR")
        self.assertEqual(access["http.status_code"], 500)
        # the two lines are joinable on more than the request
        self.assertEqual(error["event_id"], access["event_id"])

    def test_ordinary_traffic_produces_one_line_per_request(self):
        before = len(self._records())
        for _ in range(10):
            self.client.get("/health")
        new = self._records()[before:]
        self.assertEqual(len(new), 10)
        self.assertTrue(all(r["level"] == "INFO" for r in new))

    def test_every_line_is_one_json_object(self):
        for record in self._records():
            self.assertIsInstance(record, dict)
            self.assertIn("timestamp", record)

    def _records(self) -> list[dict]:
        # read the path off the module: the smoke suite may have won the
        # race to import it and bound a different temp directory
        for handler in logging.getLogger().handlers:
            if getattr(handler, "_shopflow", False):
                handler.flush()
        text = app_module.LOG_PATH.read_text().strip()
        return [json.loads(line) for line in text.splitlines()]

    def _last_log_record(self, logger: str = "shopflow.error") -> dict:
        return [r for r in self._records() if r["logger"] == logger][-1]


if __name__ == "__main__":
    unittest.main()
