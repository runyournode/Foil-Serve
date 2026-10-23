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

import io
import os
import re
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


@pytest.mark.skipif(
    shutil.which("soffice") is None, reason="LibreOffice (soffice) not installed"
)
def test_convert_spreadsheet_reports_pages_per_sheet(tmp_path):
    """The sheet→page map must account for every page of the exported PDF.

    This is the invariant the spreadsheet table of contents relies on: a sheet may
    span several pages (fit-to-width only constrains columns), and hidden sheets are
    not exported at all — a map that got either wrong would misplace every anchor.
    """
    openpyxl = pytest.importorskip("openpyxl")

    wb = openpyxl.Workbook()
    long_sheet = wb.active
    long_sheet.title = "Long"
    for row in range(400):
        long_sheet.append([f"row {row}", row, row * 2])
    wb.create_sheet("Short").append(["a", "b"])
    hidden = wb.create_sheet("Hidden")
    hidden.append(["x"])
    hidden.sheet_state = "hidden"

    src = tmp_path / "input.xlsx"
    wb.save(src)
    out = tmp_path / "out.pdf"

    srv = LibreOfficeServer(runtime_dir=str(tmp_path))
    srv.start()
    try:
        sheet_map = srv.convert_spreadsheet(
            src, out, paper_format="A4", landscape=False
        )
    finally:
        srv.stop()

    assert sheet_map is not None, "LibreOffice should report the page counts"
    assert [name for name, _ in sheet_map] == ["Long", "Short"]  # hidden sheet excluded
    assert dict(sheet_map)["Long"] > 1, "a 400-row sheet must span several A4 pages"

    # Count the page objects of the produced PDF, without pulling in a PDF library.
    n_pages = len(re.findall(rb"/Type\s*/Page[^s]", out.read_bytes()))
    assert sum(count for _, count in sheet_map) == n_pages


# ── drawings: charts and metafile pictures (requires LibreOffice) ─────────────

SPREADSHEET_FIXTURES = Path(__file__).parent / "spreadsheet" / "fixtures"
needs_soffice = pytest.mark.skipif(
    shutil.which("soffice") is None, reason="LibreOffice (soffice) not installed"
)


@pytest.fixture(scope="module")
def lo_server(tmp_path_factory):
    srv = LibreOfficeServer(runtime_dir=str(tmp_path_factory.mktemp("lo_runtime")))
    srv.start()
    yield srv
    srv.stop()


def _colors(png: Path) -> int:
    from PIL import Image

    with Image.open(png) as im:
        colors = im.convert("RGB").getcolors(maxcolors=1 << 16)
    return len(colors) if colors is not None else 1 << 16  # None: even more colors


def _input_copy(tmp_path: Path, fixture: str) -> Path:
    """Copy a fixture the way the app stores uploads (no sibling .xlsx for .xls)."""
    src = SPREADSHEET_FIXTURES / fixture
    dst = tmp_path / f"input_file{src.suffix}"
    shutil.copy(src, dst)
    return dst


@needs_soffice
def test_render_drawings_exports_charts_only(lo_server, tmp_path):
    rendered = lo_server.render_drawings(
        SPREADSHEET_FIXTURES / "drawings.xlsx",
        tmp_path / "out",
        charts=True,
        pictures=True,
    )
    assert rendered is not None
    assert [(r.sheet, r.kind, r.index, r.name) for r in rendered] == [
        ("Data", "chart", 0, "Chart 1"),
        ("Data", "chart", 1, "Chart 2"),
    ], "raster pictures are read from the file, never rendered"
    for shape in rendered:
        assert shape.png.parent == tmp_path / "out"
        assert _colors(shape.png) > 10, "a rendered chart is not a blank canvas"


@needs_soffice
def test_emf_picture_is_rasterized(lo_server, tmp_path):
    from spreadsheet_drawings import (
        decode_objects,
        render_with_libreoffice,
        survey_drawings,
    )

    path = _input_copy(tmp_path, "drawings_emf.xlsx")
    survey = survey_drawings(path, ".xlsx")
    drawings, rendered = render_with_libreoffice(
        path,
        ".xlsx",
        survey,
        lo_server,
        tmp_path,
        render_charts=True,
        rasterize_metafiles=True,
    )
    assert drawings is not None
    (encoded,) = decode_objects(drawings, rendered, 10).images.values()
    from PIL import Image

    with Image.open(io.BytesIO(encoded.jpeg)) as image:
        colors = image.convert("RGB").getcolors(maxcolors=1 << 16) or []
    red = sum(n for n, (r, g, b) in colors if r > 200 and g < 60 and b < 60)
    assert red > 0.5 * encoded.width * encoded.height, "the EMF is a red rectangle"


@needs_soffice
@pytest.mark.parametrize("fixture", ["drawings.xls", "drawings.ods"])
def test_xls_and_ods_end_to_end(lo_server, tmp_path, fixture):
    from spreadsheet_drawings import (
        decode_objects,
        render_with_libreoffice,
        survey_drawings,
    )

    path = _input_copy(tmp_path, fixture)
    survey = survey_drawings(path, path.suffix)
    drawings, rendered = render_with_libreoffice(
        path,
        path.suffix,
        survey,
        lo_server,
        tmp_path,
        render_charts=True,
        rasterize_metafiles=True,
    )
    assert drawings is not None
    result = decode_objects(drawings, rendered, 10)
    chart_1, chart_2 = (f.image for f in result.figures["Data"])
    picture_1, picture_2 = (f.image for f in result.figures["Pics"])
    assert set(result.images) == {chart_1, chart_2, picture_1, picture_2}
    assert len(result.images) == 4, "two rendered charts, two pictures"
    assert "Series: Revenue, Cost" in result.contexts[chart_1], (
        "LibreOffice-saved charts carry cached series names"
    )


RTF_FIXTURE = Path(__file__).parent / "data" / "sample.rtf"


def test_rtf_is_detected_as_office_document(tmp_path):
    from utils import PAGED_EXTS, prepare_input_file

    path, mime, raw_mime = prepare_input_file(RTF_FIXTURE.read_bytes(), tmp_path)
    assert (mime, raw_mime) == (".rtf", "text/rtf")
    assert path.suffix == ".rtf"
    assert mime in PAGED_EXTS, "converted to PDF, so page markers apply"


@pytest.mark.skipif(
    shutil.which("soffice") is None, reason="LibreOffice (soffice) not installed"
)
def test_rtf_converts_to_pdf(lo_server, tmp_path):
    from libreoffice import convert_to_pdf

    src = tmp_path / "input_file.rtf"
    shutil.copy(RTF_FIXTURE, src)
    pdf, sheet_map = convert_to_pdf(src, ".rtf", lo_server)
    assert pdf.read_bytes().startswith(b"%PDF")
    assert sheet_map is None, "not a spreadsheet"


def test_extension_families_are_the_single_source():
    """Every supported extension comes from one Literal family in utils.py."""
    from typing import get_args

    from libreoffice import _OFFICE_MIME_EXTS
    from utils import OFFICE_EXTS, SPREADSHEET_EXTS, MimeExt, mime_def

    assert {ext for ext in mime_def.values() if ext} == set(get_args(MimeExt)), (
        "mime_def and MimeExt must list the same extensions"
    )
    assert _OFFICE_MIME_EXTS == OFFICE_EXTS | SPREADSHEET_EXTS
