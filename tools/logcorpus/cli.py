"""Build the deploy-incident corpus. See README for the story it tells."""

import argparse
import json
import random
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import runner, verify, world
from .backdate import (
    BUSINESS_HOURS,
    default_rewrites,
    density_share,
    phase_windows,
    rewrite,
)
from .manifest import Manifest, ManifestPhase, validate

REPO = Path(__file__).resolve().parents[2]
DEPLOY_ROOT = "/srv/shopflow"


def load_records(path: Path) -> list[dict]:
    records = []
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{number} is not valid JSON: {exc}") from exc
    return records


def dump_records(records, path: Path) -> None:
    """Write via a temp file so a failure never leaves a half corpus."""
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_suffix(path.suffix + ".partial")
    with staging.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, default=str) + "\n")
    staging.replace(path)


def _run_phase(
    *, name, revision, version, workdir, db_path, raw_log,
    ops, bursts, seed, host, environment, args,
) -> runner.PhaseResult:
    sha = runner.resolve_sha(REPO, revision)
    snapshot = runner.export_tree(REPO, revision, workdir / name)
    env = runner.phase_env(
        snapshot, db_path=db_path, log_path=raw_log,
        version=version, commit=sha, host=host, environment=environment,
    )

    line_start = runner.count_lines(raw_log)
    pids = []
    rng = random.Random(seed)

    print(f"  {name}: {revision} ({sha[:8]}) as {version}, "
          f"{ops} ops in {bursts} burst(s)")
    with runner.serve(snapshot, env, workdir) as server:
        pids.append(server.process.pid)
        if line_start == 0:
            # a fresh world: seed history before any traffic sees it
            created = world.prepare_world(
                db_path, reference=args.deploy_at, rng=rng
            )
            print(f"    backfilled {created} historical orders")

        per_burst = max(1, ops // bursts)
        for index in range(bursts):
            runner.run_simulator(
                REPO, server.base_url,
                ops=per_burst,
                seed=runner.burst_seed(seed, name, index),
            )
            stats = world.groom_world(db_path, rng=rng)
            server.probe()
            if index == 0 or index == bursts - 1:
                shape = world.world_shape(db_path)
                print(f"    burst {index + 1}/{bursts}: "
                      f"{shape['products']} products "
                      f"({shape['paren_free_share']:.0%} paren-free), "
                      f"{shape['tier_less_share']:.0%} tier-less customers, "
                      f"stock {shape['stock_total']}")

    line_stop = runner.count_lines(raw_log)
    return runner.PhaseResult(
        name=name, revision=revision, sha=sha, version=version,
        line_start=line_start, line_stop=line_stop, pids=pids,
        snapshot=str(snapshot),
    )


def cmd_generate(args) -> int:
    workdir = Path(args.workdir) if args.workdir else Path(
        tempfile.mkdtemp(prefix="shopflow-corpus-")
    )
    workdir.mkdir(parents=True, exist_ok=True)
    db_path = workdir / "corpus.db"
    raw_log = workdir / "raw.log"

    baseline_share = density_share(
        BUSINESS_HOURS,
        args.deploy_at - timedelta(days=args.baseline_days),
        args.deploy_at,
    )
    incident_share = density_share(
        BUSINESS_HOURS,
        args.deploy_at,
        args.deploy_at + timedelta(hours=args.incident_hours),
    )
    # match the request *rate* across the boundary: a traffic step at the
    # deploy would be a second signal nobody asked for
    incident_ops = max(
        20, round(args.baseline_ops * incident_share / baseline_share)
    )

    print(f"workdir: {workdir}")
    print(f"deploy at {args.deploy_at.isoformat()}, "
          f"{args.baseline_days}d baseline / {args.incident_hours}h incident")
    if args.dry_run:
        print(f"would run {args.baseline_ops} baseline ops "
              f"and {incident_ops} incident ops")
        return 0

    phases = []
    try:
        phases.append(_run_phase(
            name="baseline", revision=args.baseline_rev,
            version=args.baseline_version, workdir=workdir, db_path=db_path,
            raw_log=raw_log, ops=args.baseline_ops, bursts=args.bursts,
            seed=args.seed, host=args.host, environment=args.env, args=args,
        ))
        phases.append(_run_phase(
            name="regression", revision=args.regression_rev,
            version=args.regression_version, workdir=workdir, db_path=db_path,
            raw_log=raw_log, ops=incident_ops,
            # Few, long bursts. Each burst is one simulator process that
            # starts with no order history of its own, and the refund bug
            # needs two refunds against one order — chopping the incident
            # into short sessions stops it firing at all.
            bursts=max(1, incident_ops // 100),
            seed=args.seed, host=args.host, environment=args.env, args=args,
        ))
    finally:
        if not args.keep_build:
            for phase in phases:
                shutil.rmtree(phase.snapshot, ignore_errors=True)

    records = load_records(raw_log)
    print(f"\ncaptured {len(records)} raw lines")

    rewrites = default_rewrites({
        f"{workdir}/baseline/src/": f"{DEPLOY_ROOT}/src/",
        f"{workdir}/regression/src/": f"{DEPLOY_ROOT}/src/",
        str(REPO / ".venv") + "/": f"{DEPLOY_ROOT}/.venv/",
        str(REPO) + "/": f"{DEPLOY_ROOT}/",
    })

    windows = phase_windows(
        args.deploy_at,
        baseline_days=args.baseline_days,
        incident_hours=args.incident_hours,
        gap_seconds=args.deploy_gap_seconds,
        line_counts=[
            (p.name, p.line_start, p.line_stop) for p in phases
        ],
    )
    retimed = rewrite(records, windows, path_rewrites=rewrites)

    manifest = Manifest(
        generated_at=datetime.now(timezone.utc).isoformat(),
        deploy_at=args.deploy_at.isoformat(),
        seed=args.seed,
        baseline_days=args.baseline_days,
        incident_hours=args.incident_hours,
        path_rewrites=[list(pair) for pair in rewrites],
        phases=[
            ManifestPhase(
                name=p.name, revision=p.revision, sha=p.sha, version=p.version,
                line_start=p.line_start, line_stop=p.line_stop, pids=p.pids,
            )
            for p in phases
        ],
    )
    validate(manifest, retimed)

    dump_records(retimed, Path(args.out))
    manifest.write(Path(args.manifest))
    print(f"wrote {args.out} and {args.manifest}")
    if not args.keep_build:
        shutil.rmtree(workdir, ignore_errors=True)

    print()
    print(verify.summarize(retimed))
    return _report(verify.run_checks(retimed, args.deploy_at))


def cmd_verify(args) -> int:
    records = load_records(Path(args.log))
    deploy_at = args.deploy_at
    if deploy_at is None:
        manifest = Manifest.read(Path(args.manifest))
        deploy_at = datetime.fromisoformat(manifest.deploy_at)
        validate(manifest, records)
        print("manifest matches the log")
    print(verify.summarize(records))
    return _report(verify.run_checks(records, deploy_at))


def _report(checks) -> int:
    print()
    for check in checks:
        print(check.render())
    failed = [c for c in checks if not c.ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    return 1 if failed else 0


def _moment(text: str) -> datetime:
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.logcorpus",
        description="Generate the shopflow deploy-incident log corpus.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    default_deploy = (
        datetime.now(timezone.utc).replace(second=0, microsecond=0)
        - timedelta(hours=6)
    )

    gen = sub.add_parser("generate", help="run both phases and write the corpus")
    gen.add_argument("--out", default="logs/app.log")
    gen.add_argument("--manifest", default="logs/corpus-manifest.json")
    # Tags, not HEAD~1: the two builds are fixed points in history, and
    # relative refs would silently slide onto the wrong commits as soon as
    # anything else lands on the branch.
    gen.add_argument("--baseline-rev", default="v1.4.2")
    gen.add_argument("--regression-rev", default="v1.5.0")
    gen.add_argument("--baseline-version", default="1.4.2")
    gen.add_argument("--regression-version", default="1.5.0")
    gen.add_argument("--baseline-ops", type=int, default=900)
    gen.add_argument("--bursts", type=int, default=12)
    gen.add_argument("--baseline-days", type=float, default=3.0)
    # Long enough that every ambient bug gets a fair sample on the far side
    # of the deploy, and a realistic time-to-detect: an afternoon deploy
    # that nobody notices until the next morning, because the endpoint only
    # half-fails and nothing alerts on it.
    gen.add_argument("--incident-hours", type=float, default=18.0)
    gen.add_argument("--deploy-gap-seconds", type=float, default=90.0)
    gen.add_argument("--deploy-at", type=_moment, default=default_deploy)
    gen.add_argument("--seed", type=int, default=20260814)
    gen.add_argument("--host", default="web-7d9c-x4k2")
    gen.add_argument("--env", default="prod")
    gen.add_argument("--workdir", default=None)
    gen.add_argument("--keep-build", action="store_true")
    gen.add_argument("--dry-run", action="store_true")
    gen.set_defaults(func=cmd_generate)

    ver = sub.add_parser("verify", help="check a corpus tells the right story")
    ver.add_argument("--log", default="logs/app.log")
    ver.add_argument("--manifest", default="logs/corpus-manifest.json")
    ver.add_argument("--deploy-at", type=_moment, default=None)
    ver.set_defaults(func=cmd_verify)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
