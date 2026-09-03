import logging
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Literal, TypeIs, get_args

from pydantic import TypeAdapter, ValidationError

from settings import PaperFormat

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# File types LibreOffice converts to PDF for us.
OfficeMimeExt = Literal[
    ".docx", ".doc", ".pptx", ".ppt", ".odt", ".odp", ".xls", ".xlsx", ".ods"
]
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


class LibreOfficeServer:
    """
    Manages a persistent LibreOffice UNO server for Office-to-PDF conversion.

    The server is started once at app startup and reused for all conversions,
    avoiding the ~3s spawn overhead on every document. If soffice crashes during
    a conversion, it is automatically restarted before retrying.

    Transport: soffice listens on a UNO **pipe**, which on Linux is a Unix domain
    socket created by the osl library (conventionally `/tmp/OSL_PIPE_<euid>_<name>`,
    though the exact directory is osl-internal). No TCP port is exposed. The pipe
    name is unique per start and namespaced with `PIPE_PREFIX`; the real socket
    path is resolved from the kernel via /proc/net/unix rather than assumed.

    Crash recovery: the soffice PID is written to a file inside `runtime_dir` so
    stale processes from a previous app crash are killed at next startup. Residual
    socket files left by a hard crash are removed by `_sweep_dead_pipes()`.
    """

    def __init__(self, runtime_dir: str = "/tmp/foil-runtime") -> None:
        self._process: subprocess.Popen | None = None
        self._pipe_name: str | None = None
        # Real filesystem path of the bound UNO socket, resolved from the kernel
        # at readiness (see _resolve_socket_path). None until soffice is ready.
        self._socket_path: Path | None = None
        self._lock = threading.Lock()
        self._pid_file = Path(runtime_dir) / "soffice.pid"

    @staticmethod
    def _make_pipe_name() -> str:
        """Build a unique, namespaced UNO pipe name (see PIPE_PREFIX).

        Uniqueness guarantees a fresh run never collides with a residual socket
        from a crashed run, and lets the cleanup sweep recognise our own sockets.
        """
        return f"{PIPE_PREFIX}{os.getpid()}_{uuid.uuid4().hex[:8]}"

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
        soffice (e.g. another worker) and is left untouched; only sockets with no
        listener are unlinked.
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

    @staticmethod
    def _is_soffice_process(pid: int) -> bool:
        """Check via /proc/{pid}/cmdline that the process is actually a LibreOffice headless server."""
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_text()
            # cmdline should be something like (on Ubuntu 24.04 at least):
            # `'/usr/lib/libreoffice/program/oosplash\x00--headless\x00--norestore\x00--accept=pipe,name=foil_soffice_...;urp;\x00'`
            t_headless = "headless" in cmdline
            t_pipe = "pipe" in cmdline and "name=" in cmdline
            t_libre_office = "libreoffice" in cmdline
            t_soffice = "soffice" in cmdline
            return t_headless and t_pipe and (t_libre_office or t_soffice)
        except OSError:
            return False

    def _cleanup_stale_processes(self) -> None:
        """Kill stale soffice processes from previous app runs using the PID file."""
        if not self._pid_file.exists():
            return
        try:
            pid = int(self._pid_file.read_text().strip())
            if not LibreOfficeServer._is_soffice_process(pid):
                logger.warning(
                    f"PID {pid} from PID file is not a soffice process — skipping kill"
                )
                return
            try:
                os.kill(pid, signal.SIGTERM)
                logger.info(f"Killed stale soffice process (pid={pid})")
                time.sleep(0.5)
            except ProcessLookupError:
                pass  # process already gone
        except (ValueError, OSError):
            pass
        finally:
            self._pid_file.unlink(missing_ok=True)

    @staticmethod
    def _drain_stderr(process: subprocess.Popen) -> None:
        """Read soffice stderr line by line, suppressing the known harmless javaldx warning."""
        assert process.stderr is not None
        for raw_line in process.stderr:
            line = raw_line.decode(errors="replace").rstrip()
            if "javaldx" in line:
                continue
            if line:
                logger.warning("soffice stderr: %s", line)

    def _wait_ready(self, timeout: float = 15.0) -> None:
        """Poll the UNO pipe until soffice accepts connections, caching the resolved path.

        The socket location is read from the kernel (_resolve_socket_path); no path
        is assumed. On success the real path is stored in _socket_path.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
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
            time.sleep(0.3)
        raise RuntimeError(
            f"soffice did not become ready within {timeout}s (pipe {self._pipe_name})"
        )

    def start(self) -> None:
        """Cleanup stale process, spawn soffice on a fresh UNO pipe, wait for readiness."""
        self._cleanup_stale_processes()

        # noinspection PyDeprecation
        if not shutil.which("soffice"):
            raise RuntimeError("soffice not found in PATH")

        self._pipe_name = self._make_pipe_name()
        self._process = subprocess.Popen(
            [
                "soffice",
                "--headless",
                "--norestore",
                f"--accept=pipe,name={self._pipe_name};urp;",
            ],
            stderr=subprocess.PIPE,
        )

        # Drain soffice stderr in a background thread, filtering out the
        # harmless "javaldx" warning (Java is not used and not required).
        threading.Thread(
            target=self._drain_stderr,
            args=(self._process,),
            daemon=True,
        ).start()

        # Persist PID so a future crash recovery can clean it up
        self._pid_file.write_text(str(self._process.pid))
        logger.info(
            f"soffice started (pid={self._process.pid}, pipe={self._pipe_name})"
        )

        self._wait_ready()  # resolves and caches self._socket_path
        # Now that the real socket directory is known, clean up residual sockets
        # left by crashed runs (never our own live socket).
        self._sweep_dead_pipes()
        logger.info(
            f"soffice ready on pipe {self._pipe_name} (socket {self._socket_path})"
        )

    def _restart(self) -> None:
        """Restart soffice after a crash."""
        logger.warning("soffice crashed, restarting...")
        # noinspection PyBroadException
        try:
            if self._process:
                self._process.terminate()
        except Exception:
            pass
        self._pid_file.unlink(missing_ok=True)
        # Drop the pipe socket of the crashed instance if it lingered.
        if self._socket_path is not None:
            self._socket_path.unlink(missing_ok=True)
            self._socket_path = None
        self.start()

    def _ensure_running(self) -> None:
        """Restart soffice if it has crashed. Thread-safe: the lock prevents
        concurrent restart attempts when multiple threads detect a crash."""
        with self._lock:
            if self._process is None or self._process.poll() is not None:
                self._restart()

    @staticmethod
    def _run_uno_script(uno_script: str, label: str = "UNO") -> None:
        """Execute a UNO Python script via the system Python, piped through stdin."""
        try:
            result = subprocess.run(
                # Use the system Python (/usr/bin/python3) rather than "python3"
                # which could resolve to the venv Python via PATH.
                ["/usr/bin/python3"],
                input=uno_script,
                capture_output=True,
                text=True,
                check=True,
            )
            if result.stdout:
                logger.debug("%s stdout: %s", label, result.stdout.strip())
            if result.stderr:
                logger.warning("%s stderr: %s", label, result.stderr.strip())
        except subprocess.CalledProcessError as e:
            logger.error(
                "%s script failed (exit code %d):\n  stdout: %s\n  stderr: %s",
                label,
                e.returncode,
                (e.output or "").strip(),
                (e.stderr or "").strip(),
            )
            raise

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

def convert():
    localContext = uno.getComponentContext()
    resolver = localContext.ServiceManager.createInstanceWithContext(
        "com.sun.star.bridge.UnoUrlResolver", localContext
    )

    ctx = resolver.resolve(
        "uno:pipe,name={self._pipe_name};urp;StarOffice.ComponentContext"
    )
    smgr = ctx.ServiceManager
    desktop = smgr.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)

    file_url = uno.systemPathToFileUrl("{file_path.resolve().as_posix()}")
    out_url  = uno.systemPathToFileUrl("{output_pdf.resolve().as_posix()}")

    # Ouvrir le document
    hidden = PropertyValue()
    hidden.Name = "Hidden"
    hidden.Value = True
    doc = desktop.loadComponentFromURL(file_url, "_blank", 0, (hidden,))

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

def convert():
    localContext = uno.getComponentContext()
    resolver = localContext.ServiceManager.createInstanceWithContext(
        "com.sun.star.bridge.UnoUrlResolver", localContext
    )

    ctx = resolver.resolve(
        "uno:pipe,name={self._pipe_name};urp;StarOffice.ComponentContext"
    )
    smgr = ctx.ServiceManager
    desktop = smgr.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)

    file_url = uno.systemPathToFileUrl("{file_path.resolve().as_posix()}")
    out_url  = uno.systemPathToFileUrl("{output_pdf.resolve().as_posix()}")

    hidden = PropertyValue()
    hidden.Name = "Hidden"
    hidden.Value = True
    doc = desktop.loadComponentFromURL(file_url, "_blank", 0, (hidden,))

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

def convert():
    localContext = uno.getComponentContext()
    resolver = localContext.ServiceManager.createInstanceWithContext(
        "com.sun.star.bridge.UnoUrlResolver", localContext
    )

    ctx = resolver.resolve(
        "uno:pipe,name={self._pipe_name};urp;StarOffice.ComponentContext"
    )
    smgr = ctx.ServiceManager
    desktop = smgr.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)

    file_url = uno.systemPathToFileUrl("{file_path.resolve().as_posix()}")
    out_url  = uno.systemPathToFileUrl("{output_xlsx.resolve().as_posix()}")

    hidden = PropertyValue()
    hidden.Name = "Hidden"
    hidden.Value = True

    doc = desktop.loadComponentFromURL(file_url, "_blank", 0, (hidden,))

    xlsx_filter = PropertyValue()
    xlsx_filter.Name = "FilterName"
    xlsx_filter.Value = "Calc MS Excel 2007 XML"

    doc.storeToURL(out_url, (xlsx_filter,))
    doc.close(True)

convert()
"""

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
        """Terminate soffice and remove the PID file."""
        if self._process:
            # noinspection PyBroadException
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except Exception:
                self._process.kill()
            finally:
                self._process = None
        self._pid_file.unlink(missing_ok=True)
        # Remove our pipe socket in case soffice did not clean it up on exit.
        if self._socket_path is not None:
            self._socket_path.unlink(missing_ok=True)
            self._socket_path = None
        logger.info("soffice stopped")


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

    if mime in (".xls", ".xlsx", ".ods"):
        try:
            sheet_map = lo_server.convert_spreadsheet(
                file_path,
                generated_pdf,
                paper_format=paper_format or "A3",
                landscape=landscape,
            )
        except Exception as e:
            raise RuntimeError(f"LibreOffice spreadsheet→PDF error: {e}") from e
    elif mime in (".docx", ".doc", ".pptx", ".ppt", ".odt", ".odp"):
        try:
            lo_server.convert_general(file_path, generated_pdf)
        except Exception as e:
            raise RuntimeError(f"LibreOffice error: {e}") from e
    else:
        raise NotImplementedError(
            f"mime type {mime!r} is not supported by convert_to_pdf."
        )

    if not generated_pdf.exists():
        raise RuntimeError("PDF conversion failed: no output file was created")

    return generated_pdf, sheet_map
