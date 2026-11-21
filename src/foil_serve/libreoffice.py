import asyncio
import contextlib
import logging
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeIs, get_args

import psutil
from pydantic import TypeAdapter, ValidationError

from settings import PaperFormat
from utils import OFFICE_EXTS, SPREADSHEET_EXTS, OfficeDocExt, SpreadsheetExt

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# File types LibreOffice converts to PDF for us.
OfficeMimeExt = Literal[OfficeDocExt, SpreadsheetExt]
_OFFICE_MIME_EXTS = frozenset(get_args(OfficeMimeExt))


def is_office_mime_ext(mime: str) -> TypeIs[OfficeMimeExt]:
    """True when `convert_to_pdf` can handle this extension (and narrows its type)."""
    return mime in _OFFICE_MIME_EXTS


# (sheet name, page count) pairs for the sheets present in a converted PDF.
# None whenever LibreOffice could not report them — callers must degrade, not guess.
SheetPageMap = list[tuple[str, int]] | None

# The sidecar is written by a separate process (soffice's own Python), so it is
# untrusted input and gets validated rather than json.load()ed blindly.
_SHEET_MAP_ADAPTER: TypeAdapter[list[tuple[str, int]]] = TypeAdapter(
    list[tuple[str, int]]
)


def _read_sheet_map(path: Path) -> SheetPageMap:
    """Parse the sheet→page-count sidecar written by the UNO script.

    Returns None when the file is missing or malformed, or when LibreOffice could
    not count the pages of at least one sheet (-1) — a partial map is worse than
    none, as it would silently misplace every anchor after the unknown sheet.
    """
    try:
        sheet_map = _SHEET_MAP_ADAPTER.validate_json(path.read_bytes())
    except (OSError, ValidationError) as e:
        logger.warning(f"Unusable sheet→page map at '{path}': {e!r}")
        return None
    if any(count < 0 for _, count in sheet_map):
        logger.warning(f"LibreOffice could not count pages for some sheets in '{path}'")
        return None
    return sheet_map


@dataclass(frozen=True, slots=True)
class RenderedShape:
    """A drawing object LibreOffice exported to PNG.

    ``index`` is the object's position among the objects of the same kind on its
    sheet, in drawing order (groups flattened) — the fallback key used to match it
    with the object found in the file's XML when shape names are ambiguous.
    """

    sheet: str
    kind: Literal["picture", "chart"]
    index: int
    name: str
    png: Path


_RENDERED_ADAPTER: TypeAdapter[list[RenderedShape]] = TypeAdapter(list[RenderedShape])


def _read_rendered(path: Path, out_dir: Path) -> list[RenderedShape] | None:
    """Parse the rendering sidecar written by the UNO script.

    Entries whose PNG is missing or lies outside ``out_dir`` are dropped: the
    sidecar comes from another process and is not trusted to point anywhere else.
    """
    try:
        shapes = _RENDERED_ADAPTER.validate_json(path.read_bytes())
    except (OSError, ValidationError) as e:
        logger.warning(f"Unusable drawing rendering sidecar at '{path}': {e!r}")
        return None
    root = out_dir.resolve()
    return [
        shape
        for shape in shapes
        if shape.png.resolve().is_relative_to(root) and shape.png.is_file()
    ]


# Resolution of drawings exported to PNG (charts, metafile pictures), and the cap
# on their longest side — enough for a VLM to read labels, small enough to ship.
_RENDER_DPI = 150
_RENDER_MAX_PX = 2000


# Prefix for our UNO pipe names. Namespacing keeps the residual-socket sweep
# (see LibreOfficeServer._sweep_dead_pipes) from ever touching LibreOffice's own
# SingleOfficeIPC pipe or pipes belonging to other applications.
PIPE_PREFIX = "foil_soffice_"

# Paper dimensions in 1/100 mm (landscape: width > height)
PAPER_SIZES: dict[PaperFormat, tuple[int, int]] = {
    "A2": (59400, 42000),
    "A3": (42000, 29700),
    "A4": (29700, 21000),
    "Letter": (27940, 21590),
    "Legal": (35560, 21590),
    "Tabloid": (43180, 27940),
}


# Prepended to every UNO script (each runs in the system Python, see _run_uno_script).
# Documents are untrusted input: before loading one, the instance is set to fetch
# nothing (a linked image or section pointing to http://... made soffice connect to it
# — server-side request forgery, and a stall until the connection times out) and run no
# macro; the load itself updates no link. NOT DisableActiveContent: it disables every
# embedded OLE object, and charts ARE OLE objects (they would vanish from the PDF).
_UNO_HARDENING = """
def harden(ctx):
    from com.sun.star.beans import PropertyValue as _PV
    provider = ctx.ServiceManager.createInstanceWithContext(
        "com.sun.star.configuration.ConfigurationProvider", ctx
    )
    node = _PV()
    node.Name = "nodepath"
    node.Value = "/org.openoffice.Office.Common/Security/Scripting"
    access = provider.createInstanceWithArguments(
        "com.sun.star.configuration.ConfigurationUpdateAccess", (node,)
    )
    access.setPropertyValue("BlockUntrustedRefererLinks", True)
    access.setPropertyValue("DisableMacrosExecution", True)
    access.commitChanges()


def safe_load_props():
    from com.sun.star.beans import PropertyValue as _PV
    props = []
    # MacroExecutionMode NEVER_EXECUTE (0), UpdateDocMode NO_UPDATE (0)
    for name, value in (("MacroExecutionMode", 0), ("UpdateDocMode", 0)):
        prop = _PV()
        prop.Name = name
        prop.Value = value
        props.append(prop)
    return tuple(props)
"""


class LibreOfficeUnavailable(RuntimeError):
    """A conversion could not finish (timeout or crash): not the document's fault.

    Carries the HTTP status the API should answer with: 504 on timeout, 503 when
    LibreOffice crashed (retryable: the next conversion runs on a fresh instance).
    """

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class LibreOfficeServer:
    """
    One LibreOffice UNO server (soffice headless), used by one conversion at a time.

    Instances are owned by a `LibreOfficePool`: a conversion gets an instance for
    itself, and the instance is restarted after every use. Nothing is ever shared
    between two conversions, so a restart can never kill someone else's work.

    Isolation between instances (several run at once):
    - its own user profile `<runtime_dir>/lo_profile_<slot>` — two live soffice on one
      profile would hand their work over to each other;
    - its own UNO **pipe** (Unix domain socket created by the osl library,
      conventionally `/tmp/OSL_PIPE_<euid>_<name>`), with a unique, namespaced name
      (`PIPE_PREFIX`); the real socket path is read from the kernel (/proc/net/unix);
    - its own process group: `/usr/bin/soffice` is a shell script that execs
      oosplash, which forks soffice.bin. Killing the started pid alone would leave
      soffice.bin running (orphaned, at 100 % CPU when wedged): the whole group is
      killed.

    Crash recovery: processes left by a previous run of the app on this slot's profile
    are killed at start; residual sockets with no listener are removed by
    `_sweep_dead_pipes()` (run once, by the pool, before the other instances start).
    """

    def __init__(
        self,
        runtime_dir: str = "/tmp/foil-runtime",
        slot: int = 0,
        conversion_timeout_s: float = 0,
    ) -> None:
        self.slot = slot
        self._process: subprocess.Popen | None = None
        self._pipe_name: str | None = None
        # Real filesystem path of the bound UNO socket, resolved from the kernel
        # at readiness (see _resolve_socket_path). None until soffice is ready.
        self._socket_path: Path | None = None
        self._profile_dir = Path(runtime_dir).resolve() / f"lo_profile_{slot}"
        self._conversion_timeout_s = conversion_timeout_s
        # stop() (event loop) and a restart (worker thread) may run at the same time:
        # the lock and `_stopped` keep a restart from starting soffice after a stop.
        self._lock = threading.Lock()
        self._stopped = False

    @staticmethod
    def _make_pipe_name(slot: int = 0) -> str:
        """Build a unique, namespaced UNO pipe name (see PIPE_PREFIX).

        Uniqueness guarantees a fresh run never collides with a residual socket
        from a crashed run, and lets the cleanup sweep recognise our own sockets.
        """
        return f"{PIPE_PREFIX}{os.getpid()}_{slot}_{uuid.uuid4().hex[:8]}"

    def _resolve_socket_path(self) -> Path | None:
        """Ask the kernel where our UNO socket is actually bound.

        Reads /proc/net/unix and returns the filesystem path whose basename ends
        with the current (unique) pipe name. This is robust: it does not assume
        where the osl library places the socket — it reads the real location from
        the kernel. Returns None if the socket is not bound yet.
        """
        if self._pipe_name is None:
            return None
        try:
            lines = Path("/proc/net/unix").read_text().splitlines()
        except OSError:
            return None
        for line in lines:
            # Trailing column is the bound path (only for pathname sockets);
            # for unbound sockets the last column is the numeric inode instead.
            last = line.rsplit(maxsplit=1)[-1] if line else ""
            if last.startswith("/") and Path(last).name.endswith(self._pipe_name):
                return Path(last)
        return None

    def _sweep_dead_pipes(self) -> None:
        """Remove orphaned UNO pipe sockets left by crashed soffice processes.

        Runs after our own socket is ready, so the directory is learned from the
        real socket path rather than assumed. Only our own namespaced sockets
        (PIPE_PREFIX) for the current euid are considered, and our own live socket
        is skipped. A socket that still accepts a connection belongs to a live
        soffice (e.g. another instance) and is left untouched; only sockets with no
        listener are unlinked. The pool runs it once, before the other instances
        start: a socket caught between bind() and listen() could look dead.
        """
        if self._socket_path is None:
            return
        for pipe in self._socket_path.parent.glob(
            f"OSL_PIPE_{os.geteuid()}_{PIPE_PREFIX}*"
        ):
            if pipe == self._socket_path:
                continue  # our own live socket
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.settimeout(0.3)
                    s.connect(str(pipe))
                # A live soffice is listening — leave it alone.
            except OSError:
                # No listener → stale socket file, safe to remove.
                try:
                    pipe.unlink()
                    logger.info(f"Removed stale UNO pipe socket {pipe}")
                except OSError:
                    pass

    def _cleanup_stale_processes(self) -> None:
        """Kill the processes a previous run of the app left on this slot's profile.

        A profile belongs to one slot, and the slot is not running yet: any process
        whose command line names it is ours and stale. Processes of other slots, or
        other LibreOffice users, are never matched.
        """
        marker = self._profile_dir.as_uri()
        for proc in psutil.process_iter(["cmdline"]):
            cmdline = proc.info["cmdline"] or []
            if any(marker in arg for arg in cmdline):
                try:
                    proc.kill()
                    proc.wait(5)
                    logger.info(
                        f"Killed stale soffice process (pid={proc.pid}, slot {self.slot})"
                    )
                except psutil.Error:
                    pass

    @staticmethod
    def _drain_stderr(process: subprocess.Popen) -> None:
        """Read soffice stderr line by line, suppressing the known harmless javaldx warning."""
        assert process.stderr is not None
        for raw_line in process.stderr:
            line = raw_line.decode(errors="replace").rstrip()
            if "javaldx" in line:
                continue
            if line:
                logger.warning(f"soffice stderr: {line}")

    def _wait_ready(self, timeout: float = 30.0) -> None:
        """Poll the UNO pipe until soffice accepts connections, caching the resolved path.

        The socket location is read from the kernel (_resolve_socket_path); no path
        is assumed. On success the real path is stored in _socket_path.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            proc = self._process
            if proc is None or proc.poll() is not None:
                raise RuntimeError(
                    f"soffice exited during start (slot {self.slot}, "
                    f"exit code {proc.returncode if proc else None})"
                )
            candidate = self._resolve_socket_path()
            if candidate is not None:
                try:
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                        s.settimeout(0.5)
                        s.connect(str(candidate))
                    self._socket_path = candidate
                    return
                except OSError:
                    pass
            time.sleep(0.1)
        raise RuntimeError(
            f"soffice did not become ready within {timeout}s (pipe {self._pipe_name})"
        )

    def start(self, sweep: bool = False) -> None:
        """Spawn soffice on a fresh UNO pipe and wait for readiness.

        `sweep`: also remove residual sockets of crashed runs (see _sweep_dead_pipes).
        """
        # noinspection PyDeprecation
        if not shutil.which("soffice"):
            raise RuntimeError("soffice not found in PATH")
        self._cleanup_stale_processes()
        self._profile_dir.mkdir(parents=True, exist_ok=True)
        # Left by a killed instance of this slot (nothing else uses the profile)
        (self._profile_dir / ".lock").unlink(missing_ok=True)

        t0 = time.perf_counter()
        with self._lock:
            if self._stopped:
                raise RuntimeError(f"soffice slot {self.slot} is stopped")
            self._pipe_name = self._make_pipe_name(self.slot)
            self._process = subprocess.Popen(
                [
                    "soffice",
                    "--headless",
                    "--norestore",  # no document recovery
                    "--nodefault",  # no empty document at start
                    "--nolockcheck",  # the profile is ours: its .lock is removed above
                    f"-env:UserInstallation={self._profile_dir.as_uri()}",
                    f"--accept=pipe,name={self._pipe_name};urp;",
                ],
                stderr=subprocess.PIPE,
                start_new_session=True,  # own process group: killed as a whole
                # No printing here: without this, the first document an instance
                # loads waits for the CUPS printer list — 30 s when localhost:631
                # drops packets instead of refusing (WSL2), paid by EVERY conversion
                # since instances are restarted after each use.
                env={**os.environ, "SAL_DISABLE_CUPS": "1"},
            )
            process = self._process

        # Drain soffice stderr in a background thread, filtering out the
        # harmless "javaldx" warning (Java is not used and not required).
        threading.Thread(
            target=self._drain_stderr, args=(process,), daemon=True
        ).start()

        try:
            self._wait_ready()  # resolves and caches self._socket_path
        except BaseException:
            self._kill()
            raise
        if sweep:
            self._sweep_dead_pipes()
        logger.info(
            f"soffice slot {self.slot} ready in {time.perf_counter() - t0:.1f}s "
            f"(pid={process.pid}, socket {self._socket_path})"
        )

    def restart(self) -> None:
        """Replace the running soffice with a fresh one (state, memory: all gone)."""
        self._kill()
        self.start()

    def _kill(self) -> None:
        """Stop this instance's whole process group: SIGTERM, then SIGKILL.

        A wedged soffice may ignore SIGTERM, and soffice.bin may outlive oosplash (the
        started process): the group is signalled until it is empty.
        """
        with self._lock:
            proc, self._process = self._process, None
            socket_path, self._socket_path = self._socket_path, None
        if proc is not None:
            pgid = proc.pid  # session leader: its pid is the group id
            for sig, grace_s in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 5.0)):
                try:
                    os.killpg(pgid, sig)
                except ProcessLookupError:
                    break  # group already empty
                deadline = time.monotonic() + grace_s
                while _group_alive(pgid) and time.monotonic() < deadline:
                    time.sleep(0.05)
                if not _group_alive(pgid):
                    break
            else:
                logger.error(
                    f"soffice group {pgid} (slot {self.slot}) survived SIGKILL"
                )
            proc.poll()  # the leader may already be reaped by _group_alive
        # Our own pipe socket, in case soffice did not clean it up on exit.
        if socket_path is not None:
            socket_path.unlink(missing_ok=True)

    def _ensure_running(self) -> None:
        """Start soffice if it is not running (died while idle)."""
        if self._process is None or self._process.poll() is not None:
            logger.warning(f"soffice slot {self.slot} is not running, restarting...")
            self.restart()

    def _run_uno_script(self, uno_script: str, label: str = "UNO") -> None:
        """Execute a UNO Python script via the system Python, piped through stdin.

        Bounded by `conversion_timeout_s` (0 = none). A timeout or a crash raises
        `LibreOfficeUnavailable`: the instance is in an unknown state, but the pool
        restarts it after every use anyway.
        """
        timeout = self._conversion_timeout_s or None
        try:
            result = subprocess.run(
                # Use the system Python (/usr/bin/python3) rather than "python3"
                # which could resolve to the venv Python via PATH.
                ["/usr/bin/python3"],
                input=uno_script,
                capture_output=True,
                text=True,
                check=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            logger.error(f"{label} timed out after {timeout:.0f}s (slot {self.slot})")
            raise LibreOfficeUnavailable(
                f"{label}: LibreOffice conversion timed out after {timeout:.0f}s",
                status_code=504,
            ) from e
        except subprocess.CalledProcessError as e:
            logger.error(
                f"{label} script failed (exit code {e.returncode}, slot {self.slot}):\n"
                f"  stdout: {(e.output or '').strip()}\n"
                f"  stderr: {(e.stderr or '').strip()}"
            )
            server_died = self._process is None or self._process.poll() is not None
            if e.returncode < 0 or server_died:
                # The UNO client died of a signal (e.g. SIGSEGV in the bridge) or
                # soffice itself is gone: not the document's fault.
                raise LibreOfficeUnavailable(
                    f"{label}: LibreOffice crashed "
                    f"({'soffice died' if server_died else f'signal {-e.returncode}'})",
                    status_code=503,
                ) from e
            raise
        if result.stdout:
            logger.debug(f"{label} stdout: {result.stdout.strip()}")
        if result.stderr:
            logger.warning(f"{label} stderr: {result.stderr.strip()}")

    def _build_uno_script_general(self, file_path: Path, output_pdf: Path) -> str:
        """
        Build a UNO Python script for general document conversion (Writer, Impress, etc.).

        This script is passed to the system Python that communicates with the
        LibreOffice headless server via the Uno bridge.
        We need a perfect match between LibreOffice <-> Uno lib <-> Python.
        If you installed LibreOffice from your distribution repo it should be fine.
        (Our venv Python is almost guaranteed to be incompatible).
        We have to do this since a simple call to `libreoffice --headless --convert-to pdf ...`
        cannot ACCEPT REVISIONS and the output PDF could be very messy.
        """
        return f"""
import uno
from com.sun.star.beans import PropertyValue
{_UNO_HARDENING}

def convert():
    localContext = uno.getComponentContext()
    resolver = localContext.ServiceManager.createInstanceWithContext(
        "com.sun.star.bridge.UnoUrlResolver", localContext
    )

    ctx = resolver.resolve(
        "uno:pipe,name={self._pipe_name};urp;StarOffice.ComponentContext"
    )
    harden(ctx)
    smgr = ctx.ServiceManager
    desktop = smgr.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)

    file_url = uno.systemPathToFileUrl("{file_path.resolve().as_posix()}")
    out_url  = uno.systemPathToFileUrl("{output_pdf.resolve().as_posix()}")

    # Ouvrir le document
    hidden = PropertyValue()
    hidden.Name = "Hidden"
    hidden.Value = True
    doc = desktop.loadComponentFromURL(
        file_url, "_blank", 0, (hidden, *safe_load_props())
    )

    # Accepter toutes les révisions (Writer uniquement)
    if doc.supportsService("com.sun.star.text.TextDocument"):
        try:
            doc.setPropertyValue("RecordRedlineChanges", False)
            doc.setPropertyValue("ShowRedlineChanges", False)
        except:
            pass
        dispatcher = smgr.createInstanceWithContext(
            "com.sun.star.frame.DispatchHelper", ctx
        )
        dispatcher.executeDispatch(
            doc.getCurrentController().getFrame(),
            ".uno:AcceptAllTrackedChanges", "", 0, ()
        )

    # Exporter en PDF
    pdf_filter = PropertyValue()
    pdf_filter.Name = "FilterName"

    if doc.supportsService("com.sun.star.text.TextDocument"):
        pdf_filter.Value = "writer_pdf_Export"
    elif doc.supportsService("com.sun.star.presentation.PresentationDocument"):
        pdf_filter.Value = "impress_pdf_Export"
    elif doc.supportsService("com.sun.star.sheet.SpreadsheetDocument"):
        pdf_filter.Value = "calc_pdf_Export"
    else:
        pdf_filter.Value = "writer_pdf_Export"

    doc.storeToURL(out_url, (pdf_filter,))
    doc.close(True)

convert()
"""

    def _build_uno_script_spreadsheet(
        self,
        file_path: Path,
        output_pdf: Path,
        sheet_map_path: Path,
        paper_format: PaperFormat = "A3",
        landscape: bool = True,
    ) -> str:
        """
        Build a UNO Python script for spreadsheet conversion: sets each sheet to
        the given paper format and orientation with fit-to-page-width before exporting to PDF.
        This avoids tiny text on sheets with many columns.

        The script also writes ``sheet_map_path`` — a JSON list of [sheet name, page
        count] for the visible sheets — so the caller can map PDF pages back to sheets.
        A page count of -1 means "unknown"; the caller then ignores the whole map.
        """
        # PAPER_SIZES stores (long_side, short_side)
        long_side, short_side = PAPER_SIZES[paper_format]
        if landscape:
            width, height = long_side, short_side
        else:
            width, height = short_side, long_side
        orientation = "landscape" if landscape else "portrait"
        return f"""
import json
import uno
from com.sun.star.beans import PropertyValue
{_UNO_HARDENING}

def convert():
    localContext = uno.getComponentContext()
    resolver = localContext.ServiceManager.createInstanceWithContext(
        "com.sun.star.bridge.UnoUrlResolver", localContext
    )

    ctx = resolver.resolve(
        "uno:pipe,name={self._pipe_name};urp;StarOffice.ComponentContext"
    )
    harden(ctx)
    smgr = ctx.ServiceManager
    desktop = smgr.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)

    file_url = uno.systemPathToFileUrl("{file_path.resolve().as_posix()}")
    out_url  = uno.systemPathToFileUrl("{output_pdf.resolve().as_posix()}")

    hidden = PropertyValue()
    hidden.Name = "Hidden"
    hidden.Value = True
    doc = desktop.loadComponentFromURL(
        file_url, "_blank", 0, (hidden, *safe_load_props())
    )

    # Set each sheet to {paper_format} {orientation}, fit all columns to 1 page width
    sheets = doc.getSheets()
    page_styles = doc.getStyleFamilies().getByName("PageStyles")
    for i in range(sheets.getCount()):
        sheet = sheets.getByIndex(i)
        style_name = sheet.PageStyle
        style = page_styles.getByName(style_name)
        style.IsLandscape = {landscape}
        style.Width = {width}    # {paper_format} {orientation} width in 1/100 mm
        style.Height = {height}   # {paper_format} {orientation} height in 1/100 mm
        style.ScaleToPagesX = 1  # fit all columns to 1 page width
        style.ScaleToPagesY = 0  # unlimited pages vertically

    # Page count per visible sheet, computed after the page styles are applied.
    # Hidden sheets are skipped: they are not part of the exported PDF.
    sheet_map = []
    for i in range(sheets.getCount()):
        sheet = sheets.getByIndex(i)
        if not sheet.IsVisible:
            continue
        try:
            n_pages = doc.getRendererCount(sheet, ())
        except Exception:
            n_pages = -1
        sheet_map.append([sheet.Name, n_pages])
    with open("{sheet_map_path.resolve().as_posix()}", "w", encoding="utf-8") as fh:
        json.dump(sheet_map, fh)

    pdf_filter = PropertyValue()
    pdf_filter.Name = "FilterName"
    pdf_filter.Value = "calc_pdf_Export"

    doc.storeToURL(out_url, (pdf_filter,))
    doc.close(True)

convert()
"""

    def _build_uno_script_xls_to_xlsx(self, file_path: Path, output_xlsx: Path) -> str:
        """Build a UNO script to convert a legacy .xls to .xlsx (handles old encryption with empty password)."""
        return f"""
import uno
from com.sun.star.beans import PropertyValue
{_UNO_HARDENING}

def convert():
    localContext = uno.getComponentContext()
    resolver = localContext.ServiceManager.createInstanceWithContext(
        "com.sun.star.bridge.UnoUrlResolver", localContext
    )

    ctx = resolver.resolve(
        "uno:pipe,name={self._pipe_name};urp;StarOffice.ComponentContext"
    )
    harden(ctx)
    smgr = ctx.ServiceManager
    desktop = smgr.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)

    file_url = uno.systemPathToFileUrl("{file_path.resolve().as_posix()}")
    out_url  = uno.systemPathToFileUrl("{output_xlsx.resolve().as_posix()}")

    hidden = PropertyValue()
    hidden.Name = "Hidden"
    hidden.Value = True

    doc = desktop.loadComponentFromURL(
        file_url, "_blank", 0, (hidden, *safe_load_props())
    )

    xlsx_filter = PropertyValue()
    xlsx_filter.Name = "FilterName"
    xlsx_filter.Value = "Calc MS Excel 2007 XML"

    doc.storeToURL(out_url, (xlsx_filter,))
    doc.close(True)

convert()
"""

    def _build_uno_script_render_drawings(
        self,
        file_path: Path,
        out_dir: Path,
        sidecar_path: Path,
        charts: bool,
        pictures: bool,
        output_xlsx: Path | None,
    ) -> str:
        """Build a UNO script exporting a spreadsheet's charts and metafile pictures to PNG.

        Walks every sheet's draw page (groups flattened, drawing order) and exports:
        - every chart when ``charts`` is set — charts are stored as XML, not as a bitmap;
        - every picture whose graphic is not a plain raster format (EMF, WMF, SVM…)
          when ``pictures`` is set — Pillow cannot draw those on Linux.
        Raster pictures are skipped: the caller reads their original bytes from the file.

        When ``output_xlsx`` is given, the document is first saved as .xlsx (used for
        legacy .xls, whose drawings are only readable once converted).

        The script writes ``sidecar_path``: a JSON list of {sheet, kind, index, name,
        png}. A shape that fails to export is skipped, not fatal.
        """
        return f"""
import json
import uno
from com.sun.star.beans import PropertyValue
{_UNO_HARDENING}

CHART_CLSID = "12DCAE26-281F-416F-A234-C3086127382E"
RASTER_MIMES = ("image/png", "image/jpeg", "image/jpg", "image/gif", "image/bmp", "image/tiff", "image/webp")
DPI = {_RENDER_DPI}
MAX_PX = {_RENDER_MAX_PX}
RENDER_CHARTS = {charts}
RENDER_PICTURES = {pictures}
OUT_DIR = {out_dir.resolve().as_posix()!r}
OUTPUT_XLSX = {output_xlsx.resolve().as_posix() if output_xlsx else None!r}


def prop(name, value):
    p = PropertyValue()
    p.Name = name
    p.Value = value
    return p


def walk(shapes):
    for i in range(shapes.getCount()):
        shape = shapes.getByIndex(i)
        if shape.getShapeType() == "com.sun.star.drawing.GroupShape":
            for child in walk(shape):
                yield child
        else:
            yield shape


def kind_of(shape):
    shape_type = shape.getShapeType()
    if shape_type == "com.sun.star.drawing.GraphicObjectShape":
        return "picture"
    if shape_type == "com.sun.star.drawing.OLE2Shape":
        try:
            if shape.CLSID.upper() == CHART_CLSID:
                return "chart"
        except Exception:
            pass
    return None


def wanted(shape, kind):
    if kind == "chart":
        return RENDER_CHARTS
    if not RENDER_PICTURES:
        return False
    try:
        mime = (shape.Graphic.MimeType or "").lower()
    except Exception:
        return False
    return mime not in RASTER_MIMES


def export_png(ctx, smgr, shape, png_path):
    size_w, size_h = shape.Size.Width, shape.Size.Height  # 1/100 mm
    if size_w < 100 or size_h < 100:
        # Degenerate size (seen on generated chartsheets): use a readable default.
        size_w, size_h = 25000, 15000
    width = max(1, int(size_w / 2540 * DPI))
    height = max(1, int(size_h / 2540 * DPI))
    scale = min(1.0, MAX_PX / max(width, height))
    width, height = max(1, int(width * scale)), max(1, int(height * scale))
    exporter = smgr.createInstanceWithContext("com.sun.star.drawing.GraphicExportFilter", ctx)
    exporter.setSourceDocument(shape)
    filter_data = uno.Any(
        "[]com.sun.star.beans.PropertyValue",
        (prop("PixelWidth", width), prop("PixelHeight", height)),
    )
    exporter.filter((
        prop("URL", uno.systemPathToFileUrl(png_path)),
        prop("MediaType", "image/png"),
        prop("FilterData", filter_data),
    ))


def render():
    localContext = uno.getComponentContext()
    resolver = localContext.ServiceManager.createInstanceWithContext(
        "com.sun.star.bridge.UnoUrlResolver", localContext
    )
    ctx = resolver.resolve(
        "uno:pipe,name={self._pipe_name};urp;StarOffice.ComponentContext"
    )
    harden(ctx)
    smgr = ctx.ServiceManager
    desktop = smgr.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)

    file_url = uno.systemPathToFileUrl({file_path.resolve().as_posix()!r})
    doc = desktop.loadComponentFromURL(
        file_url, "_blank", 0,
        (prop("Hidden", True), prop("MacroExecutionMode", 0), prop("UpdateDocMode", 0)),
    )
    rendered = []
    failures = 0
    try:
        if OUTPUT_XLSX:
            doc.storeToURL(
                uno.systemPathToFileUrl(OUTPUT_XLSX),
                (prop("FilterName", "Calc MS Excel 2007 XML"),),
            )
        sheets = doc.getSheets()
        for sheet_no in range(sheets.getCount()):
            sheet = sheets.getByIndex(sheet_no)
            counters = [0, 0]  # pictures, charts
            for shape in walk(sheet.getDrawPage()):
                kind = kind_of(shape)
                if kind is None:
                    continue
                slot = 1 if kind == "chart" else 0
                index = counters[slot]
                counters[slot] += 1
                if not wanted(shape, kind):
                    continue
                png_path = f"{{OUT_DIR}}/s{{sheet_no}}_{{kind}}{{index}}.png"
                try:
                    export_png(ctx, smgr, shape, png_path)
                except Exception:
                    failures += 1
                    continue
                rendered.append(
                    dict(sheet=sheet.Name, kind=kind, index=index, name=shape.Name, png=png_path)
                )
    finally:
        doc.close(True)

    with open({sidecar_path.resolve().as_posix()!r}, "w", encoding="utf-8") as fh:
        json.dump(rendered, fh)
    if failures:
        print(f"{{failures}} shape(s) could not be exported")

render()
"""

    def render_drawings(
        self,
        file_path: Path,
        out_dir: Path,
        charts: bool,
        pictures: bool,
        output_xlsx: Path | None = None,
    ) -> list[RenderedShape] | None:
        """Export charts and/or metafile pictures of a spreadsheet to PNG via UNO.

        See `_build_uno_script_render_drawings`. PNGs land in ``out_dir``; the
        returned list is None when the script ran but its sidecar is unusable.
        Raises when the script itself fails.
        """
        self._ensure_running()
        assert self._pipe_name is not None
        out_dir.mkdir(parents=True, exist_ok=True)
        sidecar_path = out_dir / "rendered.json"
        script = self._build_uno_script_render_drawings(
            file_path, out_dir, sidecar_path, charts, pictures, output_xlsx
        )
        self._run_uno_script(script, label="UNO render drawings")
        return _read_rendered(sidecar_path, out_dir)

    def convert_xls_to_xlsx(self, file_path: Path, output_xlsx: Path) -> None:
        """Convert a legacy .xls to .xlsx via UNO (handles old encryption with empty password)."""
        self._ensure_running()
        assert self._pipe_name is not None
        script = self._build_uno_script_xls_to_xlsx(file_path, output_xlsx)
        self._run_uno_script(script, label="UNO xls→xlsx")

    def convert_general(self, file_path: Path, output_pdf: Path) -> None:
        """Convert a document (Writer, Impress, etc.) to PDF via UNO."""
        self._ensure_running()
        assert self._pipe_name is not None
        script = self._build_uno_script_general(file_path, output_pdf)
        self._run_uno_script(script, label="UNO general")

    def convert_spreadsheet(
        self,
        file_path: Path,
        output_pdf: Path,
        paper_format: PaperFormat = "A3",
        landscape: bool = True,
    ) -> SheetPageMap:
        """Convert a spreadsheet to PDF via UNO (fit-to-width).

        Returns the (sheet name, page count) pairs of the exported sheets, or None
        when LibreOffice could not report them.
        """
        self._ensure_running()
        assert self._pipe_name is not None
        sheet_map_path = output_pdf.with_name(f"{output_pdf.stem}_sheets.json")
        script = self._build_uno_script_spreadsheet(
            file_path, output_pdf, sheet_map_path, paper_format, landscape
        )
        self._run_uno_script(script, label="UNO spreadsheet")
        return _read_sheet_map(sheet_map_path)

    def stop(self) -> None:
        """Kill soffice for good: a restart running meanwhile will not start it again."""
        with self._lock:
            self._stopped = True
        self._kill()
        logger.info(f"soffice slot {self.slot} stopped")


class LibreOfficePool:
    """`size` LibreOffice instances, each used by one conversion at a time.

    - `acquire()` hands a conversion an instance for itself (FIFO wait when all are
      busy, without blocking the event loop).
    - When the conversion is over — success, failure, timeout or cancelled caller —
      the instance is restarted in the background and only then goes back to the
      queue: every conversion starts on a fresh soffice (no memory accumulated over
      documents, no state left by a wedged or crashed conversion), and the restart
      cost stays off the request's path unless every instance is busy.
    - A long conversion holds one instance only: the others keep serving.

    No lock is needed between conversions: an instance is never shared, so a restart
    never interrupts anyone else's work.
    """

    def __init__(
        self, size: int, runtime_dir: str, conversion_timeout_s: float
    ) -> None:
        if size < 1:
            raise ValueError(f"LibreOfficePool size must be >= 1, got {size}")
        self._servers = [
            LibreOfficeServer(
                runtime_dir, slot=i, conversion_timeout_s=conversion_timeout_s
            )
            for i in range(size)
        ]
        self._idle: asyncio.Queue[LibreOfficeServer] | None = None
        self._background: set[asyncio.Task] = set()
        self._closed = False
        self._retry_delay_s = 2.0  # first wait after a failed restart, then doubled

    async def start(self) -> None:
        """Start every instance (the first one alone, then the others together)."""
        self._idle = asyncio.Queue()
        first, *others = self._servers
        try:
            # Residual sockets are swept once, by the first instance, before the
            # others start (a socket between bind() and listen() would look dead).
            await asyncio.to_thread(first.start, True)
            # The others together: a first start on a new profile takes ~15 s.
            await asyncio.gather(
                *(asyncio.to_thread(server.start) for server in others)
            )
            for server in self._servers:
                self._idle.put_nowait(server)
        except BaseException:
            self.stop()
            raise

    @contextlib.asynccontextmanager
    async def acquire(self) -> AsyncIterator[LibreOfficeServer]:
        """Exclusive use of one instance for the duration of the block."""
        if self._idle is None or self._closed:
            raise RuntimeError("LibreOffice pool is not running")
        server = await self._idle.get()
        try:
            yield server
        finally:
            self._recycle_in_background(server)

    def _recycle_in_background(self, server: LibreOfficeServer) -> None:
        if self._closed:
            return
        task = asyncio.create_task(self._recycle(server))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _recycle(self, server: LibreOfficeServer) -> None:
        assert self._idle is not None
        delay = self._retry_delay_s
        while not self._closed:
            try:
                # A conversion thread of a cancelled caller may still be talking to
                # the old soffice: it fails once it is killed, nobody waits for it.
                await asyncio.to_thread(server.restart)
                break
            except Exception as e:
                logger.error(
                    f"soffice slot {server.slot} restart failed: {e!r} — "
                    f"retrying in {delay:.0f}s"
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)
        else:
            return
        self._idle.put_nowait(server)

    def stop(self) -> None:
        """Kill every instance (a restart still running will not start it again)."""
        self._closed = True
        for task in self._background:
            task.cancel()
        for server in self._servers:
            server.stop()


def _group_alive(pgid: int) -> bool:
    """Whether process group `pgid` still has a live (non-zombie) member.

    Reaps the members that are our children first. When the app is PID 1 in a
    container (uvicorn, no init), the orphaned soffice.bin is re-parented to us: left
    unreaped, every restart would leave a zombie behind. Zombies with another parent
    are that parent's business and do not count as alive.
    """
    while True:
        try:
            pid, _ = os.waitpid(-pgid, os.WNOHANG)
        except ChildProcessError:
            break
        if pid == 0:
            break
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    for proc in psutil.process_iter():
        try:
            if os.getpgid(proc.pid) == pgid and proc.status() != psutil.STATUS_ZOMBIE:
                return True
        except (OSError, psutil.Error):
            continue
    return False


def convert_to_pdf(
    file_path: Path,
    mime: OfficeMimeExt,
    lo_server: LibreOfficeServer,
    paper_format: PaperFormat | None = None,
    landscape: bool = True,
) -> tuple[Path, SheetPageMap]:
    """
    Convert an Office document to PDF via LibreOffice UNO (output in same directory as source).
    For spreadsheets, `paper_format` and `landscape` control the page setup (default "A3",
    landscape).
    Returns the path of the generated PDF and, for spreadsheets only, the
    (sheet name, page count) pairs it contains (None when unavailable).
    """
    generated_pdf = file_path.with_suffix(".pdf")
    sheet_map: SheetPageMap = None

    if not file_path.exists():
        raise FileNotFoundError(str(file_path))

    if mime in SPREADSHEET_EXTS:
        try:
            sheet_map = lo_server.convert_spreadsheet(
                file_path,
                generated_pdf,
                paper_format=paper_format or "A3",
                landscape=landscape,
            )
        except LibreOfficeUnavailable:
            raise
        except Exception as e:
            raise RuntimeError(f"LibreOffice spreadsheet→PDF error: {e}") from e
    elif mime in OFFICE_EXTS:
        try:
            lo_server.convert_general(file_path, generated_pdf)
        except LibreOfficeUnavailable:
            raise
        except Exception as e:
            raise RuntimeError(f"LibreOffice error: {e}") from e
    else:
        raise NotImplementedError(
            f"mime type {mime!r} is not supported by convert_to_pdf."
        )

    if not generated_pdf.exists():
        raise RuntimeError("PDF conversion failed: no output file was created")

    return generated_pdf, sheet_map
