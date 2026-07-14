"""
Tests for libreoffice.py — UNO pipe (Unix domain socket) naming, kernel-based
socket-path resolution, and the residual-socket cleanup sweep.

Unit tests need neither soffice nor a GPU: real AF_UNIX sockets are created in a
short-named temp dir (kept short because AF_UNIX paths are capped at ~108 chars)
to mimic live and crashed soffice pipe files. The socket path is resolved from
the kernel via /proc/net/unix, so the tests exercise the real resolution logic.

An integration test (skipped when LibreOffice is absent) drives an actual
document → PDF conversion over the pipe using the committed fixture in data/.
"""

import os
import shutil
import socket
import tempfile
import uuid
from pathlib import Path

import pytest

from libreoffice import PIPE_PREFIX, LibreOfficeServer

FIXTURE = Path(__file__).parent / "data" / "sample.fodt"


@pytest.fixture
def short_tmp_dir():
    """A short-named temp dir under /tmp (AF_UNIX bind paths must stay < ~108 chars)."""
    d = Path(tempfile.mkdtemp(prefix="fs_", dir="/tmp"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def bound_socket():
    """Bind AF_UNIX sockets on demand and guarantee cleanup afterwards."""
    created: list[socket.socket] = []

    def _make(path: Path, *, listening: bool) -> Path:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(path))
        if listening:
            s.listen(1)  # accepts connections → looks "alive"
        # bound but not listening → connect() is refused → looks "dead"
        created.append(s)
        return path

    yield _make

    for s in created:
        s.close()


def _osl_name(pipe_name: str) -> str:
    """Socket basename as osl would create it for a given pipe name."""
    return f"OSL_PIPE_{os.geteuid()}_{pipe_name}"


# ── pipe name ─────────────────────────────────────────────────────────────────


def test_make_pipe_name_is_namespaced_and_unique():
    names = {LibreOfficeServer._make_pipe_name() for _ in range(100)}
    assert len(names) == 100  # no collisions
    assert all(n.startswith(PIPE_PREFIX) for n in names)


# ── kernel-based socket path resolution ───────────────────────────────────────


def test_resolve_socket_path_none_before_start():
    srv = LibreOfficeServer()
    assert srv._pipe_name is None
    assert srv._resolve_socket_path() is None


def test_resolve_socket_path_finds_bound_socket(short_tmp_dir, bound_socket):
    pipe_name = f"{PIPE_PREFIX}res_{uuid.uuid4().hex[:8]}"
    path = short_tmp_dir / _osl_name(pipe_name)
    bound_socket(path, listening=True)

    srv = LibreOfficeServer()
    srv._pipe_name = pipe_name

    # Resolution reads the real location from the kernel, not an assumed dir.
    assert srv._resolve_socket_path() == path


def test_resolve_socket_path_none_when_not_bound():
    srv = LibreOfficeServer()
    srv._pipe_name = f"{PIPE_PREFIX}absent_{uuid.uuid4().hex[:8]}"
    assert srv._resolve_socket_path() is None


# ── residual-socket sweep ─────────────────────────────────────────────────────


def test_sweep_removes_only_dead_own_pipes(short_tmp_dir, bound_socket):
    def mk(tag: str, *, listening: bool) -> Path:
        name = f"{PIPE_PREFIX}{tag}_{uuid.uuid4().hex[:8]}"
        return bound_socket(short_tmp_dir / _osl_name(name), listening=listening)

    own = mk("own", listening=True)  # our own live socket
    dead = mk("dead", listening=False)  # crashed sibling → must be removed
    live = mk("live", listening=True)  # another worker → must be kept
    # A dead socket outside our namespace (e.g. LibreOffice's SingleOfficeIPC)
    foreign = bound_socket(
        short_tmp_dir
        / f"OSL_PIPE_{os.geteuid()}_SingleOfficeIPC_{uuid.uuid4().hex[:6]}",
        listening=False,
    )

    srv = LibreOfficeServer()
    srv._socket_path = own

    srv._sweep_dead_pipes()

    assert own.exists(), "our own live socket must never be removed"
    assert live.exists(), "a live sibling (another worker) must be preserved"
    assert foreign.exists(), "sockets outside our namespace must be left untouched"
    assert not dead.exists(), "a dead sibling socket must be swept"


def test_sweep_noop_before_ready():
    # No socket resolved yet → nothing to scan, must not raise.
    LibreOfficeServer()._sweep_dead_pipes()


# ── integration (requires LibreOffice) ────────────────────────────────────────


@pytest.mark.skipif(
    shutil.which("soffice") is None, reason="LibreOffice (soffice) not installed"
)
def test_convert_document_over_pipe(tmp_path):
    """End-to-end: start soffice on a UNO pipe, convert the fixture to PDF, stop."""
    out = tmp_path / "out.pdf"
    srv = LibreOfficeServer(runtime_dir=str(tmp_path))
    srv.start()
    try:
        assert srv._socket_path is not None
        assert srv._socket_path.exists(), "socket resolved from the kernel must exist"
        srv.convert_general(FIXTURE, out)
        assert out.exists()
        assert out.stat().st_size > 1000
        assert out.read_bytes().startswith(b"%PDF")
    finally:
        srv.stop()

    assert srv._socket_path is None  # cleaned up on stop
