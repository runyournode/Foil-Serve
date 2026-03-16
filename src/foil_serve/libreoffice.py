import logging
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

_SOFFICE_PID_FILE = Path("/tmp/foil-serve_soffice.pid")


def _build_uno_script(file_path: Path, out_dir: Path, port: int) -> str:
    """
    This script is passed to the Python host system that will communicate with
    the LibreOffice headless server we spwaned thanks to the Uno lib.
    We need a perfect match beetween LibreOffice <-> Uno lib <-> Python.
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
        "uno:socket,host=localhost,port={port};urp;StarOffice.ComponentContext"
    )
    smgr = ctx.ServiceManager
    desktop = smgr.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)

    file_url = uno.systemPathToFileUrl("{file_path.resolve().as_posix()}")
    out_url  = uno.systemPathToFileUrl(
        "{(out_dir / (file_path.stem + ".pdf")).resolve().as_posix()}"
    )

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
            ]
        )

        # Persist PID so a future crash recovery can clean it up
        _SOFFICE_PID_FILE.write_text(str(self._process.pid))
        logger.info(f"soffice started (pid={self._process.pid}, port={self._port}) (you can safely ignore next javaldx warning)")

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
        """Restart soffice if it has crashed."""
        if self._process is None or self._process.poll() is not None:
            self._restart()

    def convert(self, file_path: Path) -> None:
        """Run the UNO conversion script against the persistent soffice server."""
        self._ensure_running()
        assert (
            self._port is not None
        )  # guaranteed by start() which _ensure_running() calls

        out_dir = file_path.parent
        uno_script = _build_uno_script(file_path, out_dir, self._port)
        with tempfile.NamedTemporaryFile(
            prefix="foilserve_UnoScript_", suffix=".py", delete=False
        ) as f:
            script_path = Path(f.name)
            script_path.write_text(uno_script)
        try:
            result = subprocess.run(
                # Use the system Python (/usr/bin/python3) rather than "python3"
                # which could resolves to the venv Python via PATH.
                ["/usr/bin/python3", script_path.as_posix()],
                capture_output=True,
                text=True,
                check=True,
            )
            if result.stdout:
                logger.debug("UNO stdout: %s", result.stdout.strip())
            if result.stderr:
                logger.warning("UNO stderr: %s", result.stderr.strip())
        except subprocess.CalledProcessError as e:
            logger.error(
                "UNO script failed (exit code %d):\n  stdout: %s\n  stderr: %s",
                e.returncode,
                (e.output or "").strip(),
                (e.stderr or "").strip(),
            )
            raise
        finally:
            script_path.unlink(missing_ok=True)

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
    mime: Literal[".docx", ".doc", ".pptx", ".ppt", ".odt", ".odp"],
    server: LibreOfficeServer,
) -> Path:
    """
    Convert an Office document to PDF via LibreOffice UNO (output in same directory as source).
    Returns the path of the generated PDF.
    """
    generated_pdf = file_path.with_suffix(".pdf")

    if mime in (".docx", ".doc", ".pptx", ".ppt", ".odt", ".odp"):
    # We may want to add a .xls and .xlsx if we decided pandas did not 
    # extract enougth data from the file (e.g., a silly spreadsheet containing
    # only text boxes and images but no actual data in cells ...) 
        if not file_path.exists():
            raise FileNotFoundError(str(file_path))
        try:
            server.convert(file_path)
        except Exception as e:
            raise RuntimeError(f"LibreOffice error: {e}") from e
    else:
        raise NotImplementedError(
            f"mime type {mime!r} is not supported by convert_to_pdf."
        )

    if not generated_pdf.exists():
        raise RuntimeError("PDF conversion failed: no output file was created")

    return generated_pdf


# Fallback using direct headless LibreOffice (documents are converted in revision mode)
# def office2pdf_libreoffice(file_path: Path):
#     subprocess.run(
#         args=[
#             'libreoffice', '--headless',
#             '--convert-to', 'pdf',
#             '--outdir', file_path.parent.as_posix(),
#             file_path.as_posix()
#         ],
#         check=True
#     )
