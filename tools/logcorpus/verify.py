"""Assert the corpus actually tells the story it is supposed to tell.

This is where correctness lives. The generator can succeed mechanically
and still produce a useless corpus — ambient bugs trending instead of
holding steady, a version that never changed, the regression firing at
the wrong rate. Each check below is a claim the README makes, tested
against the file that was actually produced.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime

from .runner import PROBE_AGENT

REGRESSION_PATH_PREFIX = "/products/"
REGRESSION_EXCEPTION = "IndexError"

# The seed data has 7 of 12 product names without a parenthesised pack
# size, so 0.583 is the floor. The live catalog runs well above it: the
# simulator's restocks create paren-free lines, and grooming can only
# retire the ones nobody ordered — a product with order history is held
# by a foreign key, exactly as it would be in a real catalog. So the
# share climbs across a long baseline and the observed rate reflects
# whatever the catalog looked like at deploy time. The story does not
# depend on the exact figure; only on it being zero before and
# substantial after.
REGRESSION_RATE_BAND = (0.45, 0.95)

# Ambient bugs must not trend across the deploy. Comparable, not equal:
# the world moves a little and the traffic mix is random.
AMBIENT_DRIFT_TOLERANCE = 0.12


@dataclass
class Check:
    name: str
    ok: bool
    detail: str

    def render(self) -> str:
        return f"  {'PASS' if self.ok else 'FAIL'}  {self.name}: {self.detail}"


def _is_probe(record: dict) -> bool:
    return record.get("http.user_agent") == PROBE_AGENT


def _is_product_detail(record: dict) -> bool:
    path = record.get("http.path", "")
    return (
        path.startswith(REGRESSION_PATH_PREFIX)
        and path.count("/") == 2
        and path[len(REGRESSION_PATH_PREFIX):].isdigit()
        and record.get("http.method") == "GET"
    )


def _split(records, deploy_at: datetime):
    before, after = [], []
    for record in records:
        moment = datetime.fromisoformat(record["timestamp"])
        (before if moment < deploy_at else after).append(record)
    return before, after


def _access(records):
    return [r for r in records if r.get("logger") == "shopflow.access"]


def _errors(records):
    return [r for r in records if r.get("logger") == "shopflow.error"]


def _rate(records, predicate) -> tuple[int, int]:
    matching = [r for r in records if predicate(r)]
    failing = [r for r in matching if r.get("http.status_code", 0) >= 500]
    return len(failing), len(matching)


def run_checks(records: list[dict], deploy_at: datetime) -> list[Check]:
    checks: list[Check] = []
    before, after = _split(records, deploy_at)

    def add(name, ok, detail):
        checks.append(Check(name, ok, detail))

    add("corpus is non-empty", bool(before) and bool(after),
        f"{len(before)} lines before, {len(after)} after the deploy")

    # --- the boundary itself -------------------------------------------
    versions_before = {r.get("version") for r in before}
    versions_after = {r.get("version") for r in after}
    add("version partitions cleanly at the deploy",
        len(versions_before) == 1 and len(versions_after) == 1
        and versions_before != versions_after,
        f"{sorted(versions_before)} -> {sorted(versions_after)}")

    commits = {r.get("commit") for r in records}
    add("both builds carry a real commit sha",
        len(commits) == 2 and all(c and c != "unknown" for c in commits),
        f"{sorted(c[:8] for c in commits if c)}")

    lifecycle = [r for r in records if r.get("logger") == "shopflow.lifecycle"]
    add("deploy is marked by lifecycle records",
        len(lifecycle) >= 2,
        f"{len(lifecycle)} records: "
        f"{[r.get('event') for r in lifecycle]}")

    # --- the regression -------------------------------------------------
    detail_before = [r for r in _access(before) if _is_product_detail(r)]
    detail_after = [r for r in _access(after) if _is_product_detail(r)]
    fails_before, total_before = _rate(detail_before, lambda r: True)
    fails_after, total_after = _rate(detail_after, lambda r: True)

    add("GET /products/{id} was healthy before the deploy",
        total_before > 0 and fails_before == 0,
        f"{fails_before}/{total_before} failed")

    rate_after = fails_after / total_after if total_after else 0.0
    add("GET /products/{id} fails at the predicted rate after",
        REGRESSION_RATE_BAND[0] <= rate_after <= REGRESSION_RATE_BAND[1],
        f"{fails_after}/{total_after} = {rate_after:.3f} "
        f"(band {REGRESSION_RATE_BAND})")

    regression_fps = {
        r["fingerprint"] for r in _errors(records)
        if r.get("exception_type") == REGRESSION_EXCEPTION
    }
    leaked = [
        r for r in _errors(before) if r.get("fingerprint") in regression_fps
    ]
    add("the regression's fingerprint is absent before the deploy",
        bool(regression_fps) and not leaked,
        f"{len(regression_fps)} fingerprint(s), {len(leaked)} pre-deploy "
        "occurrence(s)")

    # blast radius: the list endpoint shares the catalog but not the bug
    list_fails, list_total = _rate(
        _access(records),
        lambda r: r.get("http.path") == "/products" and r.get("http.method") == "GET",
    )
    add("GET /products stayed healthy throughout",
        list_total > 0 and list_fails == 0,
        f"{list_fails}/{list_total} failed")

    # --- ambient bugs must not trend -------------------------------------
    ambient = _ambient_rates(before, after, regression_fps)
    for fingerprint, (rate_b, rate_a, count_b, count_a) in sorted(ambient.items()):
        add(f"ambient {fingerprint[:8]} holds steady",
            abs(rate_a - rate_b) <= AMBIENT_DRIFT_TOLERANCE,
            f"{rate_b:.3f} -> {rate_a:.3f} ({count_b} -> {count_a} hits)")

    # Only assert both-sides presence where the incident window is long
    # enough to expect it. A bug firing at 1% of requests will legitimately
    # miss a short window, and asserting otherwise makes a flaky check.
    requests_after = len(_access(after)) or 1
    coupled = _coupled_fingerprints(records)
    expected = {
        fp: rate_b * requests_after
        for fp, (rate_b, _, _, _) in ambient.items()
    }
    assertable = {
        fp for fp, count in expected.items()
        if count >= 2.0 and fp not in coupled
    }
    absent = [fp for fp in assertable if ambient[fp][3] == 0]
    add("every ambient fingerprint still fires after the deploy",
        not absent,
        f"{len(assertable)} of {len(ambient)} fingerprint(s) frequent enough "
        f"to assert, {len(absent)} missing"
        + (f" ({[fp[:8] for fp in absent]})" if absent else ""))

    # The regression has a knock-on, and asserting the prediction is
    # stronger than exempting it. The simulator only builds a split cart —
    # the sole trigger for the stock race — after a successful read of the
    # product detail page, which is exactly what the regression breaks.
    for fingerprint in sorted(coupled & set(ambient)):
        rate_b, rate_a, count_b, count_a = ambient[fingerprint]
        add(f"stock race {fingerprint[:8]} is suppressed by the regression",
            rate_a < rate_b,
            f"{rate_b:.3f} -> {rate_a:.3f} ({count_b} -> {count_a} hits); "
            "its trigger depends on the endpoint the regression broke")

    # --- structural integrity --------------------------------------------
    add("every error line pairs with an access line",
        *_check_pairing(records))
    add("timestamps strictly increase", *_check_ordering(records))
    add("uuid7 ids agree with their own timestamps", *_check_ids(records))
    add("no developer paths leaked into tracebacks", *_check_paths(records))

    return checks


# The stock-race bug fires only via the simulator's split-cart path, which
# it reaches only after reading a product detail page successfully.
STOCK_RACE_MARKER = "stock_non_negative"


def _coupled_fingerprints(records) -> set:
    """Fingerprints whose trigger runs through the broken endpoint."""
    return {
        r["fingerprint"]
        for r in _errors(records)
        if "fingerprint" in r
        and STOCK_RACE_MARKER in str(r.get("exception_message", ""))
    }


def _ambient_rates(before, after, regression_fps) -> dict:
    """Per-fingerprint share of all requests, either side of the deploy.

    Measured against request volume rather than raw counts, because the
    two phases are different lengths.
    """
    requests_before = len(_access(before)) or 1
    requests_after = len(_access(after)) or 1

    counts_before = Counter(
        r["fingerprint"] for r in _errors(before) if "fingerprint" in r
    )
    counts_after = Counter(
        r["fingerprint"] for r in _errors(after) if "fingerprint" in r
    )

    rates = {}
    for fingerprint in set(counts_before) | set(counts_after):
        if fingerprint in regression_fps:
            continue
        count_b = counts_before.get(fingerprint, 0)
        count_a = counts_after.get(fingerprint, 0)
        rates[fingerprint] = (
            count_b / requests_before,
            count_a / requests_after,
            count_b,
            count_a,
        )
    return rates


def _check_pairing(records) -> tuple[bool, str]:
    access_by_id = defaultdict(list)
    for record in _access(records):
        access_by_id[record.get("request_id")].append(record)

    orphans = []
    for error in _errors(records):
        partners = access_by_id.get(error.get("request_id"), [])
        if not any(p.get("http.status_code") == 500 for p in partners):
            orphans.append(error.get("request_id"))
    return not orphans, f"{len(orphans)} orphaned error line(s)"


def _check_ordering(records) -> tuple[bool, str]:
    previous = None
    for index, record in enumerate(records):
        moment = datetime.fromisoformat(record["timestamp"])
        if previous is not None and moment <= previous:
            return False, f"line {index} is not after line {index - 1}"
        previous = moment
    span = (
        datetime.fromisoformat(records[-1]["timestamp"])
        - datetime.fromisoformat(records[0]["timestamp"])
    )
    return True, f"{len(records)} lines spanning {span}"


def _check_ids(records) -> tuple[bool, str]:
    """Each id should decode to roughly when its record was written.

    Not exactly: an id is minted when the request *starts* and the access
    line is written when it *ends*, so the id legitimately lags its own
    record by up to the request's duration. The bound is that duration
    plus a little slack — tight enough to catch an unretimed id, which
    would be out by years.
    """
    from .backdate import is_uuid7, uuid7_ms

    SLACK_MS = 50
    mismatched = 0
    checked = 0
    for record in records:
        moment = datetime.fromisoformat(record["timestamp"])
        record_ms = int(moment.timestamp() * 1000)
        lag_allowed = record.get("http.duration_ms", 0) + SLACK_MS
        for field in ("request_id", "event_id"):
            value = record.get(field)
            if not is_uuid7(value):
                continue
            checked += 1
            lag = record_ms - uuid7_ms(value)
            if not -SLACK_MS <= lag <= lag_allowed:
                mismatched += 1
    return mismatched == 0, f"{checked} ids checked, {mismatched} disagreed"


def _check_paths(records) -> tuple[bool, str]:
    forbidden = ("/tmp/", "/home/", "site-packages/../")
    leaks = 0
    for record in records:
        traceback = record.get("traceback", "")
        if isinstance(traceback, str) and any(f in traceback for f in forbidden):
            leaks += 1
    return leaks == 0, f"{leaks} record(s) with local paths"


def summarize(records: list[dict]) -> str:
    """A short human read on what the corpus contains."""
    access = _access(records)
    errors = _errors(records)
    fingerprints = Counter(r["fingerprint"] for r in errors if "fingerprint" in r)
    lines = [
        f"{len(records)} lines: {len(access)} access, {len(errors)} error, "
        f"{len(records) - len(access) - len(errors)} lifecycle",
        f"{len(fingerprints)} distinct fingerprints:",
    ]
    for fingerprint, count in fingerprints.most_common():
        sample = next(r for r in errors if r.get("fingerprint") == fingerprint)
        lines.append(
            f"    {fingerprint}  x{count:<4} {sample['exception_type']:<17} "
            f"{sample.get('http.method', '')} {sample.get('http.path', '')}"
        )
    return "\n".join(lines)
