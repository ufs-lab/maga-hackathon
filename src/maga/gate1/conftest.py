"""The Gate 1 harness: the fixtures that generator.CONSTRAINTS promises to the acceptance tests."""

from collections.abc import Callable, Iterator
import contextlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import cast

import pytest

PERMITTED = [5173, 5174]
HARNESS = Path(__file__).parent
_EXIT_WAIT_SECONDS = 5


def _running(pid: int) -> bool:
    """A killed process holds its port until it has exited; a zombie has already released it."""
    stat = Path(f"/proc/{pid}/stat")
    try:
        return stat.read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return False


class Backend(HTTPServer):
    """The backend stub. A test can change `allowed` to make the backend reject an Origin."""

    origins: list[str | None]
    allowed: list[str]


class _Health(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # the name that BaseHTTPRequestHandler calls
        stub = cast("Backend", self.server)
        origin = self.headers.get("Origin")
        stub.origins.append(origin)
        self.send_response(HTTPStatus.OK if origin in stub.allowed else HTTPStatus.FORBIDDEN)
        self.end_headers()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "packages/config").mkdir(parents=True)
    (tmp_path / "packages/config/ports.json").write_text(json.dumps({"frontend_ports": PERMITTED}))
    (tmp_path / "apps/web").mkdir(parents=True)
    (tmp_path / "apps/web/package.json").write_text("{}")
    (tmp_path / "apps/api/src").mkdir(parents=True)
    origins = [f"http://localhost:{port}" for port in PERMITTED]
    (tmp_path / "apps/api/src/server.js").write_text(f"const allowed = {json.dumps(origins)};\n")
    return tmp_path


@pytest.fixture
def backend() -> Iterator[Backend]:
    server = Backend(("127.0.0.1", 4000), _Health)
    server.origins = []
    server.allowed = [f"http://localhost:{port}" for port in PERMITTED]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def occupy() -> Iterator[Callable[[int], socket.socket]]:
    sockets: list[socket.socket] = []

    def _occupy(port: int) -> socket.socket:
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen()
        sockets.append(listener)
        return listener

    yield _occupy
    for listener in sockets:
        listener.close()


@pytest.fixture
def run(
    repo: Path, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Callable[..., subprocess.CompletedProcess[str]]]:
    # The vite stand-in writes its PID here, so cleanup never depends on the script under test.
    registry = tmp_path_factory.mktemp("harness") / "vite_pids"
    env = {
        **os.environ,
        "PATH": f"{HARNESS}{os.pathsep}{os.environ['PATH']}",
        "MAGA_VITE_PIDS": str(registry),
    }

    def _run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, os.environ["MAGA_SCRIPT"], *args],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    yield _run
    pids = [int(pid) for pid in registry.read_text().split()] if registry.exists() else []
    for pid in pids:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    deadline = time.monotonic() + _EXIT_WAIT_SECONDS
    while any(_running(pid) for pid in pids) and time.monotonic() < deadline:
        time.sleep(0.01)  # a bounded wait for the exit, which frees the port for the next test
