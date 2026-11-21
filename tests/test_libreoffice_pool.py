"""LibreOfficePool: one conversion per instance, a fresh soffice after every use.

Unit tests drive the pool with fake instances; the integration tests (skipped without
LibreOffice) check what only a real soffice can show: the whole process group is
killed (no orphaned soffice.bin), a timeout answers 504 and replaces the instance, and
an instance's restart never touches another one.
"""

import asyncio
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock

import psutil
import pytest

import libreoffice
from libreoffice import (
    LibreOfficePool,
    LibreOfficeServer,
    LibreOfficeUnavailable,
)

DATA = Path(__file__).parent / "data"
needs_soffice = pytest.mark.skipif(
    shutil.which("soffice") is None, reason="LibreOffice (soffice) not installed"
)


@pytest.fixture(scope="module")
def lo_runtime(tmp_path_factory) -> str:
    """Shared runtime dir: profiles are reused (a first start on a new profile takes
    ~15 s, a start on an existing one ~1 s)."""
    return str(tmp_path_factory.mktemp("lo_runtime"))


def _run(coro):
    return asyncio.run(coro)


# ── unit: fake instances ─────────────────────────────────────────────────────


class _FakeServer:
    def __init__(self, slot: int, fail_restarts: int = 0) -> None:
        self.slot = slot
        self.starts = 0
        self.restarts = 0
        self.stopped = False
        self.busy = False
        self._fail_restarts = fail_restarts

    def start(self, sweep: bool = False) -> None:
        self.starts += 1
        self.swept = sweep

    def restart(self) -> None:
        assert not self.busy, "restarted while a conversion uses it"
        if self._fail_restarts:
            self._fail_restarts -= 1
            raise RuntimeError("soffice did not start")
        self.restarts += 1

    def stop(self) -> None:
        self.stopped = True


def _fake_pool(*servers: _FakeServer) -> LibreOfficePool:
    pool = LibreOfficePool(len(servers), runtime_dir="/unused", conversion_timeout_s=0)
    pool._servers = list(servers)
    pool._retry_delay_s = 0.01
    return pool


def test_only_first_instance_sweeps():
    a, b, c = _FakeServer(0), _FakeServer(1), _FakeServer(2)
    pool = _fake_pool(a, b, c)
    _run(pool.start())
    assert (a.swept, b.swept, c.swept) == (True, False, False)
    pool.stop()
    assert a.stopped and b.stopped and c.stopped


def test_instances_are_exclusive_and_recycled_after_each_use():
    servers = [_FakeServer(0), _FakeServer(1)]
    pool = _fake_pool(*servers)

    async def convert(log: list) -> None:
        async with pool.acquire() as lo:
            assert not lo.busy
            lo.busy = True
            log.append(lo.slot)
            await asyncio.sleep(0.02)
            lo.busy = False

    async def scenario():
        await pool.start()
        log: list[int] = []
        await asyncio.gather(*(convert(log) for _ in range(6)))
        # let the last background restarts finish
        await asyncio.sleep(0.05)
        return log

    log = _run(scenario())
    assert len(log) == 6
    assert sum(s.restarts for s in servers) == 6  # one fresh soffice per conversion
    pool.stop()


def test_failed_conversion_still_recycles():
    server = _FakeServer(0)
    pool = _fake_pool(server)

    async def scenario():
        await pool.start()
        with pytest.raises(ValueError):
            async with pool.acquire():
                raise ValueError("bad document")
        async with pool.acquire():  # waits for the restart, then gets it again
            pass

    _run(scenario())
    assert server.restarts >= 1
    pool.stop()


def test_failed_restart_is_retried():
    server = _FakeServer(0, fail_restarts=2)
    pool = _fake_pool(server)

    async def scenario():
        await pool.start()
        async with pool.acquire():
            pass
        async with asyncio.timeout(5):
            async with pool.acquire():
                pass

    _run(scenario())
    assert server.restarts >= 1
    pool.stop()


def test_acquire_after_stop_raises():
    pool = _fake_pool(_FakeServer(0))

    async def scenario():
        await pool.start()
        pool.stop()
        with pytest.raises(RuntimeError, match="not running"):
            async with pool.acquire():
                pass

    _run(scenario())


def test_crashed_uno_client_answers_503(monkeypatch):
    srv = LibreOfficeServer(slot=0)
    srv._process = MagicMock()
    srv._process.poll.return_value = None  # soffice itself still alive

    def _segfault(*args, **kwargs):
        raise subprocess.CalledProcessError(-11, args[0], output="", stderr="")

    monkeypatch.setattr(libreoffice.subprocess, "run", _segfault)
    with pytest.raises(LibreOfficeUnavailable) as exc:
        srv._run_uno_script("pass", label="UNO test")
    assert exc.value.status_code == 503


def test_document_error_is_not_unavailable(monkeypatch):
    srv = LibreOfficeServer(slot=0)
    srv._process = MagicMock()
    srv._process.poll.return_value = None

    def _fails(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0], output="", stderr="IOException")

    monkeypatch.setattr(libreoffice.subprocess, "run", _fails)
    with pytest.raises(subprocess.CalledProcessError):
        srv._run_uno_script("pass", label="UNO test")


# ── integration: real soffice ────────────────────────────────────────────────


def _group_pids(server: LibreOfficeServer) -> set[int]:
    assert server._process is not None
    pgid = server._process.pid
    pids = set()
    for proc in psutil.process_iter():
        try:
            if os.getpgid(proc.pid) == pgid and proc.status() != psutil.STATUS_ZOMBIE:
                pids.add(proc.pid)
        except (OSError, psutil.Error):
            pass
    return pids


def _soffice_bin(server: LibreOfficeServer) -> psutil.Process:
    for pid in _group_pids(server):
        proc = psutil.Process(pid)
        if proc.name().startswith("soffice.bin"):
            return proc
    raise AssertionError("no soffice.bin in the instance's group")


def _alive(pids: set[int]) -> set[int]:
    alive = set()
    for pid in pids:
        try:
            if psutil.Process(pid).status() != psutil.STATUS_ZOMBIE:
                alive.add(pid)
        except psutil.NoSuchProcess:
            pass
    return alive


@needs_soffice
def test_two_instances_convert_in_parallel(tmp_path, lo_runtime):
    pool = LibreOfficePool(2, runtime_dir=lo_runtime, conversion_timeout_s=120)

    async def convert(name: str, ext: str):
        src = tmp_path / f"{name}{ext}"
        shutil.copy(DATA / f"sample{ext}", src)
        pdf = src.with_suffix(".pdf")
        async with pool.acquire() as lo:
            await asyncio.to_thread(lo.convert_general, src, pdf)
            return pdf, lo.slot

    async def scenario():
        await pool.start()
        try:
            return await asyncio.gather(convert("a", ".fodt"), convert("b", ".rtf"))
        finally:
            pool.stop()

    (pdf_a, slot_a), (pdf_b, slot_b) = _run(scenario())
    assert pdf_a.stat().st_size > 0 and pdf_b.stat().st_size > 0
    assert {slot_a, slot_b} == {0, 1}


@needs_soffice
def test_stop_kills_the_whole_group(tmp_path, lo_runtime):
    srv = LibreOfficeServer(lo_runtime, slot=0)
    srv.start()
    pids = _group_pids(srv)
    assert any(psutil.Process(p).name().startswith("soffice.bin") for p in pids)
    srv.stop()
    assert not _alive(pids), "soffice.bin must not survive its instance"


@needs_soffice
def test_timeout_answers_504_and_restart_spares_other_instance(tmp_path, lo_runtime):
    wedged = LibreOfficeServer(lo_runtime, slot=0, conversion_timeout_s=3)
    other = LibreOfficeServer(lo_runtime, slot=1, conversion_timeout_s=60)
    wedged.start(sweep=True)
    other.start()
    try:
        other_pids = _group_pids(other)
        wedged_pids = _group_pids(wedged)
        _soffice_bin(wedged).send_signal(signal.SIGSTOP)  # a livelocked soffice

        src = tmp_path / "doc.fodt"
        shutil.copy(DATA / "sample.fodt", src)
        t0 = time.monotonic()
        with pytest.raises(LibreOfficeUnavailable) as exc:
            wedged.convert_general(src, tmp_path / "doc.pdf")
        assert exc.value.status_code == 504
        assert time.monotonic() - t0 < 30

        wedged.restart()  # what the pool does after every use
        assert not _alive(wedged_pids), "the stopped soffice.bin must be killed"
        assert _alive(other_pids) == other_pids, "another instance must be untouched"

        # both serve again
        out = tmp_path / "again.fodt"
        shutil.copy(DATA / "sample.fodt", out)
        wedged.convert_general(out, tmp_path / "again.pdf")
        rtf = tmp_path / "other.rtf"
        shutil.copy(DATA / "sample.rtf", rtf)
        other.convert_general(rtf, tmp_path / "other.pdf")
    finally:
        wedged.stop()
        other.stop()


@needs_soffice
def test_stale_processes_of_the_slot_are_killed_at_start(tmp_path, lo_runtime):
    crashed_run = LibreOfficeServer(lo_runtime, slot=0)
    crashed_run.start()
    stale = _group_pids(crashed_run)
    neighbour = LibreOfficeServer(lo_runtime, slot=1)
    neighbour.start()
    neighbour_pids = _group_pids(neighbour)
    try:
        # the app restarts without having stopped slot 0
        fresh = LibreOfficeServer(lo_runtime, slot=0)
        fresh.start()
        try:
            assert not _alive(stale), "stale processes of slot 0 must be killed"
            assert _alive(neighbour_pids) == neighbour_pids, "slot 1 must be untouched"
        finally:
            fresh.stop()
    finally:
        crashed_run._process = None  # already killed
        neighbour.stop()


@needs_soffice
def test_first_conversion_after_start_is_fast(tmp_path, lo_runtime):
    """Every conversion runs on a freshly started instance: the first document load
    must not wait for anything (it waited 30 s for the CUPS printer list on WSL2)."""
    srv = LibreOfficeServer(lo_runtime, slot=0)
    srv.start()
    try:
        src = tmp_path / "doc.rtf"
        shutil.copy(DATA / "sample.rtf", src)
        t0 = time.monotonic()
        srv.convert_general(src, tmp_path / "doc.pdf")
        assert time.monotonic() - t0 < 10
    finally:
        srv.stop()


_LINKED_IMAGE_FODT = """<?xml version="1.0" encoding="UTF-8"?>
<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
 xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
 xmlns:xlink="http://www.w3.org/1999/xlink"
 xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0"
 office:version="1.3" office:mimetype="application/vnd.oasis.opendocument.text">
 <office:body><office:text>
  <text:p>A document linking external resources.</text:p>
  <text:p><draw:frame draw:name="img" svg:width="5cm" svg:height="5cm" text:anchor-type="paragraph">
   <draw:image xlink:href="http://127.0.0.1:{port}/pixel.png" xlink:type="simple"
    xlink:show="embed" xlink:actuate="onLoad"/>
  </draw:frame></text:p>
  <text:section text:name="linked">
   <text:section-source xlink:href="http://127.0.0.1:{port}/doc.odt" xlink:type="simple"/>
  </text:section>
 </office:text></office:body></office:document>
"""


@needs_soffice
def test_documents_cannot_make_soffice_fetch_urls(tmp_path, lo_runtime):
    """Server-side request forgery guard: a linked image or section must not make
    soffice connect anywhere (it did before BlockUntrustedRefererLinks)."""
    import socket
    import threading

    hits: list[str] = []
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(0.2)
    port = listener.getsockname()[1]
    done = threading.Event()

    def accept() -> None:
        while not done.is_set():
            try:
                conn, _ = listener.accept()
            except OSError:
                continue
            hits.append(conn.recv(200).decode(errors="replace"))
            conn.close()

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    srv = LibreOfficeServer(lo_runtime, slot=0)
    srv.start()
    try:
        src = tmp_path / "linked.fodt"
        src.write_text(_LINKED_IMAGE_FODT.format(port=port))
        srv.convert_general(src, tmp_path / "linked.pdf")
        assert (tmp_path / "linked.pdf").stat().st_size > 0
    finally:
        srv.stop()
        done.set()
        thread.join()
        listener.close()
    assert not hits, f"soffice fetched a URL from the document: {hits}"
