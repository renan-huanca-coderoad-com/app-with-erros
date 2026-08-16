"""Where each phase's records start and stop, and a check that it's true.

Line offsets come from the runner, which knows them exactly. They are
then cross-checked against the ``version``, ``commit`` and ``pid`` on the
records themselves — offsets alone would not catch the failure that
matters most, a phase whose ``SHOPFLOW_VERSION`` silently did not take
effect, leaving a "regression" corpus where both halves claim the same
build.
"""

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence


@dataclass
class ManifestPhase:
    name: str
    revision: str
    sha: str
    version: str
    line_start: int
    line_stop: int
    pids: list[int]


@dataclass
class Manifest:
    generated_at: str
    deploy_at: str
    seed: int
    baseline_days: float
    incident_hours: float
    path_rewrites: list[list[str]]
    phases: list[ManifestPhase]

    def to_json(self) -> str:
        payload = asdict(self)
        return json.dumps(payload, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "Manifest":
        payload = json.loads(text)
        payload["phases"] = [ManifestPhase(**p) for p in payload["phases"]]
        return cls(**payload)

    def write(self, path: Path) -> None:
        path.write_text(self.to_json() + "\n")

    @classmethod
    def read(cls, path: Path) -> "Manifest":
        return cls.from_json(path.read_text())


def validate(manifest: Manifest, records: Sequence[dict]) -> None:
    """Fail loudly if the manifest and the records disagree. Never repair."""
    phases = manifest.phases
    if not phases:
        raise ValueError("manifest has no phases")

    if phases[0].line_start != 0:
        raise ValueError("first phase must start at line 0")
    if phases[-1].line_stop != len(records):
        raise ValueError(
            f"phases cover {phases[-1].line_stop} lines, log has {len(records)}"
        )
    for earlier, later in zip(phases, phases[1:]):
        if earlier.line_stop != later.line_start:
            raise ValueError(
                f"phases {earlier.name} and {later.name} do not tile: "
                f"{earlier.line_stop} != {later.line_start}"
            )

    seen_versions = set()
    for phase in phases:
        span = records[phase.line_start:phase.line_stop]
        if not span:
            raise ValueError(f"phase {phase.name} covers no records")
        for offset, record in enumerate(span):
            index = phase.line_start + offset
            if record.get("version") != phase.version:
                raise ValueError(
                    f"line {index} in phase {phase.name} reports version "
                    f"{record.get('version')!r}, expected {phase.version!r} "
                    "(did SHOPFLOW_VERSION reach the server?)"
                )
            if record.get("commit") != phase.sha:
                raise ValueError(
                    f"line {index} in phase {phase.name} reports commit "
                    f"{record.get('commit')!r}, expected {phase.sha!r}"
                )
        if phase.version in seen_versions:
            raise ValueError(f"version {phase.version} used by two phases")
        seen_versions.add(phase.version)
