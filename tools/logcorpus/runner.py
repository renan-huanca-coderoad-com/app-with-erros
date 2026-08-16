"""Run the app at two different commits, into one log file.

The app is exported at each revision with ``git archive`` rather than
checked out with ``git worktree``: a worktree writes into the user's
``.git`` and needs removing, pruning and an atexit hook to stay tidy,
and a hard kill leaves a stale registration behind. An archive opens the
repository read-only and leaves a plain directory that a temp dir cleans
up for free.
"""

import os
import signal
import socket
import subprocess
import sys
import time
import zlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import httpx

READY_TIMEOUT = 30.0
SHUTDOWN_GRACE = 15.0

# Health probes land in the corpus, which is realistic — every service
# behind a load balancer has them. Tagging them makes it possible to
# exclude them from error-rate arithmetic later.
PROBE_AGENT = "kube-probe/1.29"


class ServerDied(RuntimeError):
    pass


class HardKill(RuntimeError):
    pass


@dataclass
class PhaseResult:
    name: str
    revision: str
    sha: str
    version: str
    line_start: int
    line_stop: int
    pids: list[int] = field(default_factory=list)
    snapshot: str = ""


def resolve_sha(repo: Path, revision: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", revision],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def export_tree(repo: Path, revision: str, dest: Path) -> Path:
    """Extract the tree at `revision`. The repository is only ever read."""
    dest.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(
        ["git", "-C", str(repo), "archive", revision],
        check=True, stdout=subprocess.PIPE,
    ).stdout
    subprocess.run(["tar", "-x", "-C", str(dest)], input=archive, check=True)
    return dest


def free_port() -> int:
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("rb") as handle:
        return sum(1 for _ in handle)


@dataclass
class ServerHandle:
    process: subprocess.Popen
    port: int

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def probe(self) -> None:
        """A load-balancer style health check, recorded in the corpus."""
        try:
            httpx.get(
                f"{self.base_url}/health",
                timeout=2.0,
                headers={"user-agent": PROBE_AGENT},
            )
        except httpx.HTTPError:
            pass


def _wait_ready(process: subprocess.Popen, port: int, stderr_path: Path) -> None:
    deadline = time.monotonic() + READY_TIMEOUT
    client = httpx.Client(timeout=1.0, headers={"user-agent": PROBE_AGENT})
    while time.monotonic() < deadline:
        if process.poll() is not None:
            tail = stderr_path.read_text(errors="replace")[-2000:]
            raise ServerDied(f"server exited during startup:\n{tail}")
        try:
            if client.get(f"http://127.0.0.1:{port}/health").status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise TimeoutError(f"server not ready on port {port} after {READY_TIMEOUT}s")


def _stop(process: subprocess.Popen) -> None:
    """SIGTERM, then wait. A hard kill is a failure, not a fallback.

    The logging handler flushes on every record, so the only way to get a
    truncated final line is to kill the process mid-write — and a corpus
    with a torn last line is worse than no corpus.
    """
    if process.poll() is not None:
        return
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=SHUTDOWN_GRACE)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
        raise HardKill("server ignored SIGTERM; the log may be truncated")


@contextmanager
def serve(snapshot: Path, env: dict, workdir: Path):
    port = free_port()
    stdout_path = workdir / "uvicorn.out"
    stderr_path = workdir / "uvicorn.err"
    argv = [
        sys.executable, "-m", "uvicorn", "shopflow.app:app",
        # --app-dir lands at sys.path[0], ahead of even PYTHONPATH, so the
        # snapshot wins over the venv's editable install of the checkout
        "--app-dir", str(snapshot / "src"),
        "--host", "127.0.0.1", "--port", str(port),
        "--log-level", "warning", "--no-access-log",
        "--timeout-graceful-shutdown", "10",
    ]
    with stdout_path.open("ab") as out, stderr_path.open("ab") as err:
        process = subprocess.Popen(
            argv, env=env, stdout=out, stderr=err, cwd=str(snapshot)
        )
        try:
            _wait_ready(process, port, stderr_path)
            yield ServerHandle(process, port)
        finally:
            _stop(process)


def phase_env(
    snapshot: Path, *, db_path: Path, log_path: Path,
    version: str, commit: str, host: str, environment: str,
) -> dict:
    """Environment for one phase's server process.

    Two settings here are load-bearing rather than cosmetic:

    * ``SHOPFLOW_VERSION`` must be set. Left unset, the app falls back to
      ``importlib.metadata``, which reads the *checkout's* installed
      metadata even while running snapshot code — both phases would
      report the same version and the deploy boundary would vanish.
    * The paths must be absolute. Importing the app builds it at module
      scope, and the defaults resolve against the process's cwd.
    """
    return {
        **os.environ,
        "PYTHONPATH": str(snapshot / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "SHOPFLOW_DB": str(db_path.resolve()),
        "SHOPFLOW_LOG": str(log_path.resolve()),
        "SHOPFLOW_VERSION": version,
        "SHOPFLOW_COMMIT": commit,
        "SHOPFLOW_HOST": host,
        "SHOPFLOW_ENV": environment,
    }


def run_simulator(repo: Path, base_url: str, *, ops: int, seed: int) -> None:
    subprocess.run(
        [
            sys.executable, str(repo / "simulator" / "shopper.py"),
            "--base-url", base_url,
            "--ops", str(ops),
            "--seed", str(seed),
            "--delay", "0",
        ],
        check=True, capture_output=True, text=True, cwd=str(repo),
    )


def burst_seed(base_seed: int, phase_name: str, index: int) -> int:
    """Independent RNG streams per burst, derived from one seed.

    Deriving rather than continuing one stream keeps each phase's traffic
    mix reproducible on its own terms. crc32 rather than hash(): string
    hashing is salted per process, which would quietly make every run
    different while still looking seeded.
    """
    salt = zlib.crc32(phase_name.encode())
    return (base_seed * 1_000_003 + salt * 31 + index) % (2**31)
