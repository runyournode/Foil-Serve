import logging
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

_SOFFICE_PID_FILE = Path("/tmp/foil-serve_soffice.pid")


class LibreOfficeServer:
    """
    Manages a persistent LibreOffice UNO server for Office-to-PDF conversion.

    The server is started once at app startup and reused for all conversions,
    avoiding the ~3s spawn overhead on every document. If soffice crashes during
    a conversion, it is automatically restarted before retrying.

    PID persistence: the soffice PID is written to _SOFFICE_PID_FILE so that
    stale processes from previous app crashes are cleaned up at next startup.
    """

    def __init__(self) -> None:
        self._process: subprocess.Popen | None = None
        self._port: int | None = None
        self._lock = threading.Lock()

    @staticmethod
    def _find_free_port() -> int:
        with socket.socket() as s:
            s.bind(("", 0))
            return s.getsockname()[1]

    @staticmethod
    def _is_soffice_process(pid: int) -> bool:
        """Check via /proc/{pid}/cmdline that the process is actually a LibreOffice headless server."""
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_text()
            # cmdline should be something like (on Ubuntu 24.04 at least):
            # `'/usr/lib/libreoffice/program/oosplash\x00--headless\x00--norestore\x00--accept=socket,host=localhost,port=59411;urp;\x00'`
            t_headless = "headless" in cmdline
            t_port = "port=" in cmdline
            t_libre_office = "libreoffice" in cmdline
            t_soffice = "soffice" in cmdline
            return t_headless and t_port and (t_libre_office or t_soffice)
        except OSError:
            return False

    @staticmethod
    def _cleanup_stale_processes() -> None:
        """Kill stale soffice processes from previous app runs using the PID file."""
        if not _SOFFICE_PID_FILE.exists():
            return
        try:
            pid = int(_SOFFICE_PID_FILE.read_text().strip())
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
            _SOFFICE_PID_FILE.unlink(missing_ok=True)

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
        """Poll the UNO socket until soffice accepts connections."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("localhost", self._port), timeout=0.5):
                    return
            except (ConnectionRefusedError, OSError):
                time.sleep(0.3)
        raise RuntimeError(
            f"soffice did not become ready within {timeout}s on port {self._port}"
        )

    def start(self) -> None:
        """Cleanup any stale process, pick a free port, spawn soffice, wait for readiness."""
        self._cleanup_stale_processes()

        # noinspection PyDeprecation
        if not shutil.which("soffice"):
            raise RuntimeError("soffice not found in PATH")

        self._port = self._find_free_port()
        self._process = subprocess.Popen(
            [
                "soffice",
                "--headless",
                "--norestore",
                f"--accept=socket,host=localhost,port={self._port};urp;",
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
        _SOFFICE_PID_FILE.write_text(str(self._process.pid))
        logger.info(f"soffice started (pid={self._process.pid}, port={self._port})")

        self._wait_ready()
        logger.info(f"soffice ready on port {self._port}")

    def _restart(self) -> None:
        """Restart soffice after a crash."""
        logger.warning("soffice crashed, restarting...")
        # noinspection PyBroadException
        try:
            if self._process:
                self._process.terminate()
        except Exception:
            pass
        _SOFFICE_PID_FILE.unlink(missing_ok=True)
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
        "uno:socket,host=localhost,port={self._port};urp;StarOffice.ComponentContext"
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

    def _build_uno_script_spreadsheet(self, file_path: Path, output_pdf: Path) -> str:
        """
        Build a UNO Python script for spreadsheet conversion: sets each sheet to
        A3 landscape with fit-to-page-width before exporting to PDF.
        This avoids tiny text on sheets with many columns.
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
        "uno:socket,host=localhost,port={self._port};urp;StarOffice.ComponentContext"
    )
    smgr = ctx.ServiceManager
    desktop = smgr.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)

    file_url = uno.systemPathToFileUrl("{file_path.resolve().as_posix()}")
    out_url  = uno.systemPathToFileUrl("{output_pdf.resolve().as_posix()}")

    hidden = PropertyValue()
    hidden.Name = "Hidden"
    hidden.Value = True
    doc = desktop.loadComponentFromURL(file_url, "_blank", 0, (hidden,))

    # Set each sheet to A3 landscape, fit all columns to 1 page width
    sheets = doc.getSheets()
    page_styles = doc.getStyleFamilies().getByName("PageStyles")
    for i in range(sheets.getCount()):
        sheet = sheets.getByIndex(i)
        style_name = sheet.PageStyle
        style = page_styles.getByName(style_name)
        style.IsLandscape = True
        style.Width = 42000    # A3 width in 1/100 mm
        style.Height = 29700   # A3 height in 1/100 mm
        style.ScaleToPagesX = 1  # fit all columns to 1 page width
        style.ScaleToPagesY = 0  # unlimited pages vertically

    pdf_filter = PropertyValue()
    pdf_filter.Name = "FilterName"
    pdf_filter.Value = "calc_pdf_Export"

    doc.storeToURL(out_url, (pdf_filter,))
    doc.close(True)

convert()
"""

    def convert_general(self, file_path: Path, output_pdf: Path) -> None:
        """Convert a document (Writer, Impress, etc.) to PDF via UNO."""
        self._ensure_running()
        assert self._port is not None
        script = self._build_uno_script_general(file_path, output_pdf)
        self._run_uno_script(script, label="UNO general")

    def convert_spreadsheet(self, file_path: Path, output_pdf: Path) -> None:
        """Convert a spreadsheet to PDF via UNO (A3 landscape, fit-to-width)."""
        self._ensure_running()
        assert self._port is not None
        script = self._build_uno_script_spreadsheet(file_path, output_pdf)
        self._run_uno_script(script, label="UNO spreadsheet")

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
        _SOFFICE_PID_FILE.unlink(missing_ok=True)
        logger.info("soffice stopped")


def convert_to_pdf(
    file_path: Path,
    mime: Literal[
        ".docx", ".doc", ".pptx", ".ppt", ".odt", ".odp", ".xls", ".xlsx", ".ods"
    ],
    server: LibreOfficeServer,
) -> Path:
    """
    Convert an Office document to PDF via LibreOffice UNO (output in same directory as source).
    Returns the path of the generated PDF.
    """
    generated_pdf = file_path.with_suffix(".pdf")

    if not file_path.exists():
        raise FileNotFoundError(str(file_path))

    if mime in (".xls", ".xlsx", ".ods"):
        try:
            server.convert_spreadsheet(file_path, generated_pdf)
        except Exception as e:
            raise RuntimeError(f"LibreOffice spreadsheet→PDF error: {e}") from e
    elif mime in (".docx", ".doc", ".pptx", ".ppt", ".odt", ".odp"):
        try:
            server.convert_general(file_path, generated_pdf)
        except Exception as e:
            raise RuntimeError(f"LibreOffice error: {e}") from e
    else:
        raise NotImplementedError(
            f"mime type {mime!r} is not supported by convert_to_pdf."
        )

    if not generated_pdf.exists():
        raise RuntimeError("PDF conversion failed: no output file was created")

    return generated_pdf
