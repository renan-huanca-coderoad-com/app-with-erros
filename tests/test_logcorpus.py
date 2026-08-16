"""Tests for the log-corpus backdater.

The generator shells out to a real server; the timeline logic does not.
That split is the whole reason the raw log and the retiming are separate
steps — everything worth asserting is a pure function over records.
"""

import copy
import unittest
import uuid
from datetime import datetime, timedelta, timezone

from tools.logcorpus.backdate import (
    BUSINESS_HOURS,
    Density,
    Phase,
    build_id_map,
    density_share,
    is_uuid7,
    phase_windows,
    plan_timestamps,
    retime_uuid7,
    rewrite,
    rewrite_paths,
    uuid7_at,
    uuid7_ms,
)
from tools.logcorpus.manifest import Manifest, ManifestPhase, validate

BASE = datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc)


def _uuid7(seed: int, ms: int = 1_755_000_000_000) -> str:
    return uuid7_at(ms, (seed * 2_654_435_761) | (1 << 63))


def make_access(index, request_id, *, offset_ms=0, status=200, event_id=None):
    record = {
        "timestamp": (BASE + timedelta(milliseconds=offset_ms)).isoformat(),
        "level": "INFO",
        "logger": "shopflow.access",
        "message": f"GET /products/{index} {status}",
        "version": "1.4.2",
        "commit": "a" * 40,
        "request_id": request_id,
        "http.method": "GET",
        "http.path": f"/products/{index}",
        "http.status_code": status,
    }
    if event_id:
        record["event_id"] = event_id
    return record


def make_error(index, request_id, event_id, *, offset_ms=0):
    return {
        "timestamp": (BASE + timedelta(milliseconds=offset_ms)).isoformat(),
        "level": "ERROR",
        "logger": "shopflow.error",
        "message": "unhandled exception in GET /products",
        "version": "1.4.2",
        "commit": "a" * 40,
        "request_id": request_id,
        "event_id": event_id,
        "fingerprint": "deadbeefdeadbeef",
        "exception_type": "IndexError",
        "traceback": (
            'File "/tmp/shopflow-corpus-x1/baseline/src/shopflow/routes/'
            'catalog.py", line 41, in get_product\n'
        ),
    }


def one_phase(records, *, days=3.0, density=None):
    return [Phase("baseline", 0, len(records), BASE, BASE + timedelta(days=days),
                  density)]


class Uuid7RetimeTest(unittest.TestCase):
    def test_retimed_id_is_still_a_valid_uuid7(self):
        retimed = retime_uuid7(_uuid7(1), 1_800_000_000_000)
        parsed = uuid.UUID(retimed)
        self.assertEqual(parsed.version, 7)
        self.assertEqual(parsed.variant, uuid.RFC_4122)

    def test_retimed_id_decodes_to_the_requested_moment(self):
        moment_ms = 1_800_000_123_456
        self.assertEqual(uuid7_ms(retime_uuid7(_uuid7(2), moment_ms)), moment_ms)

    def test_randomness_is_preserved_so_ids_stay_distinct(self):
        original = _uuid7(3)
        retimed = retime_uuid7(original, 1_800_000_000_000)
        low_mask = (1 << 80) - 1
        self.assertEqual(uuid.UUID(retimed).int & low_mask,
                         uuid.UUID(original).int & low_mask)

    def test_distinct_inputs_stay_distinct_at_the_same_moment(self):
        retimed = {retime_uuid7(_uuid7(i), 1_800_000_000_000) for i in range(500)}
        self.assertEqual(len(retimed), 500)

    def test_retiming_is_deterministic(self):
        self.assertEqual(retime_uuid7(_uuid7(4), 999_000),
                         retime_uuid7(_uuid7(4), 999_000))

    def test_non_uuid7_values_are_not_mistaken_for_ids(self):
        for value in ("-", "trace-abc-123", "", None, 42,
                      str(uuid.uuid4())):
            with self.subTest(value=value):
                self.assertFalse(is_uuid7(value))


class PlanTimestampsTest(unittest.TestCase):
    def test_output_is_strictly_increasing(self):
        records = [make_access(i, _uuid7(i), offset_ms=i * 5) for i in range(50)]
        planned = plan_timestamps(records, one_phase(records))
        self.assertEqual(planned, sorted(planned))
        self.assertEqual(len(set(planned)), len(planned))

    def test_records_land_inside_their_window(self):
        records = [make_access(i, _uuid7(i), offset_ms=i * 5) for i in range(20)]
        phases = one_phase(records, days=3.0)
        planned = plan_timestamps(records, phases)
        self.assertGreaterEqual(planned[0], phases[0].window_start)
        self.assertLessEqual(planned[-1], phases[0].window_end + timedelta(seconds=1))

    def test_order_survives_a_clock_step_during_generation(self):
        # index position, not the recorded time, is the ordering authority
        records = [make_access(i, _uuid7(i), offset_ms=i * 5) for i in range(10)]
        records[5]["timestamp"] = (BASE - timedelta(hours=2)).isoformat()
        planned = plan_timestamps(records, one_phase(records))
        self.assertEqual(planned, sorted(planned))

    def test_error_and_access_line_stay_milliseconds_apart(self):
        request_id, event_id = _uuid7(7), _uuid7(8)
        records = [
            make_error(1, request_id, event_id, offset_ms=0),
            make_access(1, request_id, offset_ms=2, status=500, event_id=event_id),
        ]
        # pad so the phase has real reach to stretch across
        records += [make_access(i, _uuid7(100 + i), offset_ms=100 + i)
                    for i in range(30)]
        planned = plan_timestamps(records, one_phase(records))
        self.assertEqual(planned[1] - planned[0], timedelta(milliseconds=2))

    def test_a_record_outside_every_phase_is_an_error(self):
        records = [make_access(i, _uuid7(i)) for i in range(5)]
        short = [Phase("baseline", 0, 3, BASE, BASE + timedelta(days=1))]
        with self.assertRaises(ValueError) as caught:
            plan_timestamps(records, short)
        self.assertIn("not covered by any phase", str(caught.exception))

    def test_lifecycle_records_without_a_request_are_placed(self):
        records = [
            {"timestamp": BASE.isoformat(), "logger": "shopflow.lifecycle",
             "request_id": "-", "version": "1.4.2", "commit": "a" * 40},
            make_access(1, _uuid7(1), offset_ms=10),
            {"timestamp": (BASE + timedelta(milliseconds=20)).isoformat(),
             "logger": "shopflow.lifecycle", "request_id": "-",
             "version": "1.4.2", "commit": "a" * 40},
        ]
        planned = plan_timestamps(records, one_phase(records))
        self.assertEqual(len(planned), 3)
        self.assertEqual(planned, sorted(planned))


class DensityTest(unittest.TestCase):
    def test_shaped_output_is_still_monotone(self):
        records = [make_access(i, _uuid7(i), offset_ms=i) for i in range(200)]
        planned = plan_timestamps(
            records, one_phase(records, density=BUSINESS_HOURS)
        )
        self.assertEqual(planned, sorted(planned))

    def test_traffic_concentrates_in_working_hours(self):
        records = [make_access(i, _uuid7(i), offset_ms=i) for i in range(400)]
        shaped = plan_timestamps(
            records, one_phase(records, days=7.0, density=BUSINESS_HOURS)
        )
        flat = plan_timestamps(records, one_phase(records, days=7.0))

        def daytime_share(moments):
            return sum(1 for m in moments if 8 <= m.hour < 19) / len(moments)

        self.assertGreater(daytime_share(shaped), daytime_share(flat))
        self.assertGreater(daytime_share(shaped), 0.6)

    def test_weights_are_validated(self):
        with self.assertRaises(ValueError):
            Density(hour_weights=(1.0,), weekday_weights=(1.0,) * 7)
        with self.assertRaises(ValueError):
            Density(hour_weights=(1.0,) * 24, weekday_weights=(1.0,) * 3)

    def test_density_share_scales_with_window_length(self):
        day = density_share(BUSINESS_HOURS, BASE, BASE + timedelta(days=1))
        hour = density_share(BUSINESS_HOURS, BASE, BASE + timedelta(hours=1))
        self.assertGreater(day, hour)


class IdMapTest(unittest.TestCase):
    def setUp(self):
        self.request_id, self.event_id = _uuid7(11), _uuid7(12)
        self.records = [
            make_error(1, self.request_id, self.event_id, offset_ms=0),
            make_access(1, self.request_id, offset_ms=2, status=500,
                        event_id=self.event_id),
        ] + [make_access(i, _uuid7(200 + i), offset_ms=50 + i) for i in range(30)]
        self.planned = plan_timestamps(self.records, one_phase(self.records))

    def test_a_request_keeps_one_id_across_both_its_lines(self):
        rewritten = rewrite(self.records, one_phase(self.records))
        self.assertEqual(rewritten[0]["request_id"], rewritten[1]["request_id"])
        self.assertEqual(rewritten[0]["event_id"], rewritten[1]["event_id"])
        self.assertNotEqual(rewritten[0]["request_id"], self.request_id)

    def test_distinct_requests_never_collide(self):
        rewritten = rewrite(self.records, one_phase(self.records))
        ids = [r["request_id"] for r in rewritten]
        self.assertEqual(len(set(ids)), len(set(
            r["request_id"] for r in self.records
        )))

    def test_event_id_is_minted_just_after_its_request_id(self):
        mapping = build_id_map(self.records, self.planned)
        self.assertEqual(
            uuid7_ms(mapping[self.event_id]) - uuid7_ms(mapping[self.request_id]),
            1,
        )
        self.assertLess(mapping[self.request_id], mapping[self.event_id])

    def test_ids_agree_with_their_own_timestamps(self):
        rewritten = rewrite(self.records, one_phase(self.records))
        for record in rewritten:
            moment = datetime.fromisoformat(record["timestamp"])
            expected = int(moment.timestamp() * 1000)
            self.assertLessEqual(
                abs(uuid7_ms(record["request_id"]) - expected), 5
            )


class RewriteTest(unittest.TestCase):
    def setUp(self):
        self.records = [
            make_error(1, _uuid7(21), _uuid7(22), offset_ms=0),
            make_access(1, _uuid7(21), offset_ms=2, status=500),
        ] + [make_access(i, _uuid7(300 + i), offset_ms=20 + i) for i in range(20)]

    def test_inputs_are_never_mutated(self):
        before = copy.deepcopy(self.records)
        rewrite(self.records, one_phase(self.records))
        self.assertEqual(self.records, before)

    def test_no_timestamp_loses_its_fractional_part(self):
        # isoformat() drops microseconds entirely at exactly zero, which
        # would leave one record shaped unlike all its neighbours
        rewritten = rewrite(self.records, one_phase(self.records))
        for record in rewritten:
            self.assertNotEqual(
                datetime.fromisoformat(record["timestamp"]).microsecond, 0
            )

    def test_local_paths_are_replaced_in_tracebacks(self):
        rewritten = rewrite(
            self.records, one_phase(self.records),
            path_rewrites=[("/tmp/shopflow-corpus-x1/baseline/src/",
                            "/srv/shopflow/src/")],
        )
        traceback = rewritten[0]["traceback"]
        self.assertIn("/srv/shopflow/src/shopflow/routes/catalog.py", traceback)
        self.assertNotIn("/tmp/", traceback)

    def test_path_rewriting_leaves_the_fingerprint_alone(self):
        # fingerprints are computed from package-relative frames, so a path
        # rewrite must not regroup anything
        rewritten = rewrite(
            self.records, one_phase(self.records),
            path_rewrites=[("/tmp/shopflow-corpus-x1/baseline/src/",
                            "/srv/shopflow/src/")],
        )
        self.assertEqual(rewritten[0]["fingerprint"],
                         self.records[0]["fingerprint"])

    def test_records_without_a_traceback_survive_rewriting(self):
        rewritten = rewrite(
            self.records, one_phase(self.records),
            path_rewrites=[("/tmp/", "/srv/")],
        )
        self.assertNotIn("traceback", rewritten[1])

    def test_longest_root_wins_when_roots_nest(self):
        rewrites = rewrite_paths(
            "/a/b/c/file.py",
            sorted({"/a/": "/X/", "/a/b/c/": "/Y/"}.items(),
                   key=lambda item: -len(item[0])),
        )
        self.assertEqual(rewrites, "/Y/file.py")


class PhaseWindowsTest(unittest.TestCase):
    def test_baseline_ends_before_the_deploy_and_incident_starts_on_it(self):
        deploy = datetime(2026, 8, 14, 10, 5, tzinfo=timezone.utc)
        baseline, incident = phase_windows(
            deploy, baseline_days=3.0, incident_hours=6.0, gap_seconds=90,
            line_counts=[("baseline", 0, 10), ("regression", 10, 20)],
        )
        self.assertEqual(baseline.window_end, deploy - timedelta(seconds=90))
        self.assertEqual(incident.window_start, deploy)
        self.assertEqual(baseline.window_end - baseline.window_start,
                         timedelta(days=3))
        self.assertLess(baseline.window_end, incident.window_start)

    def test_incident_phase_is_not_diurnally_shaped(self):
        _, incident = phase_windows(
            BASE, baseline_days=3.0, incident_hours=6.0, gap_seconds=90,
            line_counts=[("baseline", 0, 10), ("regression", 10, 20)],
        )
        self.assertIsNone(incident.density)

    def test_two_phases_are_required(self):
        with self.assertRaises(ValueError):
            phase_windows(BASE, baseline_days=1, incident_hours=1,
                          gap_seconds=1, line_counts=[("only", 0, 5)])


class ManifestTest(unittest.TestCase):
    def _manifest(self, **overrides):
        phases = overrides.pop("phases", [
            ManifestPhase("baseline", "HEAD~1", "a" * 40, "1.4.2", 0, 2, [1]),
            ManifestPhase("regression", "HEAD", "b" * 40, "1.5.0", 2, 4, [2]),
        ])
        return Manifest(
            generated_at=BASE.isoformat(), deploy_at=BASE.isoformat(),
            seed=1, baseline_days=3.0, incident_hours=6.0,
            path_rewrites=[], phases=phases, **overrides,
        )

    def _records(self):
        return [
            {"version": "1.4.2", "commit": "a" * 40},
            {"version": "1.4.2", "commit": "a" * 40},
            {"version": "1.5.0", "commit": "b" * 40},
            {"version": "1.5.0", "commit": "b" * 40},
        ]

    def test_a_consistent_manifest_validates(self):
        validate(self._manifest(), self._records())

    def test_a_version_that_never_took_effect_is_caught(self):
        # the failure mode line offsets alone cannot see: SHOPFLOW_VERSION
        # silently ignored, so both halves claim the same build
        records = self._records()
        records[2]["version"] = "1.4.2"
        with self.assertRaises(ValueError) as caught:
            validate(self._manifest(), records)
        self.assertIn("SHOPFLOW_VERSION", str(caught.exception))

    def test_phases_must_tile_the_log(self):
        phases = [
            ManifestPhase("baseline", "HEAD~1", "a" * 40, "1.4.2", 0, 2, [1]),
            ManifestPhase("regression", "HEAD", "b" * 40, "1.5.0", 3, 4, [2]),
        ]
        with self.assertRaises(ValueError) as caught:
            validate(self._manifest(phases=phases), self._records())
        self.assertIn("do not tile", str(caught.exception))

    def test_a_short_manifest_is_rejected(self):
        phases = [
            ManifestPhase("baseline", "HEAD~1", "a" * 40, "1.4.2", 0, 2, [1]),
        ]
        with self.assertRaises(ValueError):
            validate(self._manifest(phases=phases), self._records())

    def test_round_trips_through_json(self):
        manifest = self._manifest()
        restored = Manifest.from_json(manifest.to_json())
        self.assertEqual(restored.phases[1].sha, "b" * 40)
        self.assertEqual(restored.deploy_at, manifest.deploy_at)


if __name__ == "__main__":
    unittest.main()
