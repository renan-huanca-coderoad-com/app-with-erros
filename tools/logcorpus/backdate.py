"""Move a generated log onto a realistic timeline. Pure — no I/O.

Traffic is generated in a couple of minutes; the corpus needs to read as
days. Everything here is a function of parsed records plus a phase
layout, so the expensive step (actually running the app) happens once and
the timeline can be re-cut as often as you like.

Two things make this more than multiplying timestamps:

* The UUIDv7 identifiers encode their own creation time. Rewriting
  ``timestamp`` alone would leave ids that decode to the real generation
  instant, contradicting the record they sit on.
* An error line and its access line are one request. They share a
  ``request_id``, and the gap between them is single-digit milliseconds.
  Stretching a two-minute phase across three days is a ~2000x inflation,
  which would push that pair seconds apart while ``http.duration_ms``
  still says 3.86.
"""

import bisect
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Mapping, Sequence

Record = dict

# Everything below the 48-bit millisecond field: the version nibble, the
# variant bits and the 74 bits of randomness. Preserving it keeps a
# retimed id valid and distinct.
_RAND_MASK = (1 << 80) - 1
_MS_MASK = 0xFFFF_FFFF_FFFF


def is_uuid7(value: object) -> bool:
    """True for values this module may safely retime."""
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return parsed.version == 7


def uuid7_at(ms: int, entropy: int) -> str:
    """A UUIDv7 stamped at `ms`, carrying `entropy`'s low bits.

    Same layout as ``observability.uuid7``, with both halves supplied
    rather than sampled. The version and variant bits are re-stamped
    rather than assumed: they live inside the low 80 bits, so entropy
    that did not come from a real v7 would otherwise yield an id that
    every downstream check quietly refuses to recognise.
    """
    value = ((ms & _MS_MASK) << 80) | (entropy & _RAND_MASK)
    value &= ~(0xF << 76)       # version nibble
    value |= 0x7 << 76
    value &= ~(0b11 << 62)      # variant bits
    value |= 0b10 << 62
    return str(uuid.UUID(int=value))


def retime_uuid7(value: str, ms: int) -> str:
    """Re-stamp an existing id, keeping its randomness so it stays unique."""
    return uuid7_at(ms, uuid.UUID(value).int)


def uuid7_ms(value: str) -> int:
    """The millisecond timestamp an id encodes."""
    return uuid.UUID(value).int >> 80


def _to_ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


@dataclass(frozen=True)
class Density:
    """A traffic shape: relative weight per hour of day and day of week.

    A flat baseline is the most obvious synthetic tell there is — plot
    requests per hour and you get a rectangle. Real traffic breathes.
    """

    hour_weights: tuple[float, ...]
    weekday_weights: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.hour_weights) != 24:
            raise ValueError("hour_weights must have 24 entries")
        if len(self.weekday_weights) != 7:
            raise ValueError("weekday_weights must have 7 entries")
        if min(self.hour_weights) < 0 or min(self.weekday_weights) < 0:
            raise ValueError("weights must be non-negative")


# Weekday office traffic: quiet overnight, ramping from 06:00, flat across
# the working day, tailing off after 18:00. Weekends much lighter.
BUSINESS_HOURS = Density(
    hour_weights=(
        0.15, 0.12, 0.10, 0.10, 0.12, 0.20,   # 00-05
        0.40, 0.70, 1.00, 1.15, 1.20, 1.15,   # 06-11
        0.95, 1.10, 1.20, 1.15, 1.05, 0.90,   # 12-17
        0.65, 0.45, 0.35, 0.28, 0.22, 0.18,   # 18-23
    ),
    weekday_weights=(1.0, 1.0, 1.0, 1.0, 0.95, 0.35, 0.30),
)


class _Warper:
    """Maps a 0..1 position onto a window, following a Density curve.

    Precomputes the cumulative distribution over one-minute bins once,
    then answers each lookup with a bisect — the alternative is
    recomputing the curve for every record.
    """

    def __init__(self, start: datetime, end: datetime, density: Density | None):
        self.start = start
        self.span = (end - start).total_seconds()
        self.density = density
        if density is None:
            return

        bins = max(1, int(self.span // 60))
        self._edges: list[float] = []
        cumulative = 0.0
        for index in range(bins):
            moment = start + timedelta(seconds=index * self.span / bins)
            cumulative += (
                density.hour_weights[moment.hour]
                * density.weekday_weights[moment.weekday()]
            )
            self._edges.append(cumulative)
        total = self._edges[-1] or 1.0
        self._edges = [value / total for value in self._edges]
        self._bins = bins

    def at(self, fraction: float) -> datetime:
        fraction = min(max(fraction, 0.0), 1.0)
        if self.density is None:
            return self.start + timedelta(seconds=fraction * self.span)
        index = bisect.bisect_left(self._edges, fraction)
        index = min(index, self._bins - 1)
        return self.start + timedelta(seconds=index * self.span / self._bins)


@dataclass(frozen=True)
class Phase:
    """One contiguous run of records, and the window it should occupy."""

    name: str
    line_start: int
    line_stop: int
    window_start: datetime
    window_end: datetime
    density: Density | None = None


def _parse(record: Record) -> datetime:
    return datetime.fromisoformat(record["timestamp"])


def _group_records(
    records: Sequence[Record], start: int, stop: int
) -> list[list[int]]:
    """Bucket record indices by request, in order of first appearance.

    Grouped by ``request_id`` *identity* rather than by adjacency: the two
    lines of one request are adjacent today, but would not be if the
    generator ever drives concurrent clients, and grouping by identity
    costs nothing extra.
    """
    by_id: dict[str, list[int]] = defaultdict(list)
    order: list[str] = []
    for index in range(start, stop):
        request_id = records[index].get("request_id", "-")
        # lifecycle and library lines belong to no request; each is its own
        key = request_id if is_uuid7(request_id) else f"\x00singleton:{index}"
        if key not in by_id:
            order.append(key)
        by_id[key].append(index)
    return [by_id[key] for key in order]


def plan_timestamps(
    records: Sequence[Record],
    phases: Sequence[Phase],
    *,
    min_gap_ms: int = 3,
) -> list[datetime]:
    """One new timestamp per record. Strictly increasing, order preserved.

    Position in the file — not the recorded time — is the ordering
    authority, so a clock step during generation cannot reorder anything.
    """
    planned: list[datetime | None] = [None] * len(records)

    for phase in phases:
        groups = _group_records(records, phase.line_start, phase.line_stop)
        if not groups:
            continue

        warper = _Warper(phase.window_start, phase.window_end, phase.density)
        first_anchor = groups[0][0]
        last_anchor = groups[-1][0]
        reach = max(1, last_anchor - first_anchor)

        previous_ms: int | None = None
        for group in groups:
            anchor = group[0]
            moment = warper.at((anchor - first_anchor) / reach)

            # never let one group land on or before the last
            anchor_ms = _to_ms(moment)
            if previous_ms is not None and anchor_ms < previous_ms + min_gap_ms:
                anchor_ms = previous_ms + min_gap_ms
                moment = datetime.fromtimestamp(anchor_ms / 1000, timezone.utc)
            previous_ms = anchor_ms

            # keep the real sub-millisecond shape inside the request
            anchor_real = _parse(records[anchor])
            for index in group:
                offset = _parse(records[index]) - anchor_real
                if offset < timedelta(0):
                    offset = timedelta(0)
                planned[index] = moment + offset

    missing = [i for i, value in enumerate(planned) if value is None]
    if missing:
        raise ValueError(
            f"{len(missing)} record(s) not covered by any phase, "
            f"first at index {missing[0]}"
        )
    return planned  # type: ignore[return-value]


def build_id_map(
    records: Sequence[Record], planned: Sequence[datetime]
) -> dict[str, str]:
    """Old identifier -> new identifier, one entry per distinct id.

    Remapping by identity is what keeps an error line and its access line
    joined: they look up the same key, so they cannot drift apart.
    """
    request_ms: dict[str, int] = {}
    event_ms: dict[str, int] = {}

    for record, moment in zip(records, planned):
        request_id = record.get("request_id")
        if is_uuid7(request_id) and request_id not in request_ms:
            request_ms[request_id] = _to_ms(moment)
        event_id = record.get("event_id")
        if is_uuid7(event_id) and event_id not in event_ms:
            # the request id is minted first; keep that ordering visible
            event_ms[event_id] = _to_ms(moment) + 1

    mapping = {
        old: retime_uuid7(old, ms)
        for old, ms in list(request_ms.items()) + list(event_ms.items())
    }
    if len(set(mapping.values())) != len(mapping):
        raise ValueError("id remapping collided; two ids share one new value")
    return mapping


def rewrite_paths(text: str, rewrites: Sequence[tuple[str, str]]) -> str:
    for old, new in rewrites:
        text = text.replace(old, new)
    return text


def rewrite(
    records: Sequence[Record],
    phases: Sequence[Phase],
    *,
    path_rewrites: Sequence[tuple[str, str]] = (),
    min_gap_ms: int = 3,
) -> list[Record]:
    """Return retimed copies. Inputs are never mutated."""
    planned = plan_timestamps(records, phases, min_gap_ms=min_gap_ms)
    id_map = build_id_map(records, planned)

    out: list[Record] = []
    for record, moment in zip(records, planned):
        fresh = dict(record)
        if moment.microsecond == 0:
            # isoformat() drops the fractional part entirely at exactly zero,
            # leaving one record shaped unlike every other
            moment = moment.replace(microsecond=1)
        fresh["timestamp"] = moment.isoformat()

        for field in ("request_id", "event_id"):
            value = fresh.get(field)
            if isinstance(value, str) and value in id_map:
                fresh[field] = id_map[value]

        if path_rewrites:
            for field in ("traceback", "exception_message"):
                if isinstance(fresh.get(field), str):
                    fresh[field] = rewrite_paths(fresh[field], path_rewrites)

        out.append(fresh)
    return out


def phase_windows(
    deploy_at: datetime,
    *,
    baseline_days: float,
    incident_hours: float,
    gap_seconds: float,
    line_counts: Iterable[tuple[str, int, int]],
    density: Density | None = BUSINESS_HOURS,
) -> list[Phase]:
    """Lay two phases either side of a deploy instant.

    The baseline stops `gap_seconds` short of the deploy — the quiet
    moment while a rollout is in flight — and the incident starts on it.
    """
    counts = list(line_counts)
    if len(counts) != 2:
        raise ValueError("expected exactly two phases")

    gap = timedelta(seconds=gap_seconds)
    baseline_end = deploy_at - gap
    baseline_start = baseline_end - timedelta(days=baseline_days)

    (name_a, start_a, stop_a), (name_b, start_b, stop_b) = counts
    return [
        Phase(name_a, start_a, stop_a, baseline_start, baseline_end, density),
        Phase(
            name_b,
            start_b,
            stop_b,
            deploy_at,
            deploy_at + timedelta(hours=incident_hours),
            None,  # hours long, so a daily curve has nothing to say
        ),
    ]


def density_share(
    density: Density | None, start: datetime, end: datetime
) -> float:
    """Total traffic weight over a window, in arbitrary units.

    Used to size the incident phase so the *request rate* is continuous
    across the deploy. Without it the deploy would also step the traffic
    volume, which is a second signal nobody asked for.
    """
    if density is None:
        return (end - start).total_seconds() / 3600.0
    total = 0.0
    moment = start
    while moment < end:
        total += (
            density.hour_weights[moment.hour]
            * density.weekday_weights[moment.weekday()]
        ) / 60.0
        moment += timedelta(minutes=1)
    return total


def default_rewrites(roots: Mapping[str, str]) -> list[tuple[str, str]]:
    """Longest-first, so nested roots cannot be partially rewritten."""
    return sorted(roots.items(), key=lambda item: -len(item[0]))
