"""Route-level tests for the spreadsheet strategy dispatch in `process_document`.

Only the pandas-side outcomes are exercised: anything reaching the OCR pipeline
needs a live PaddleOCR-VL endpoint. That is enough to pin the decision table —
which mode runs pandas, which one may fall back, and what fails with which status
code — which is exactly where the logic is easy to get wrong.

`processing` is imported lazily (it pulls PaddleOCR in) and the whole module is
skipped when that import is not available.
"""

import asyncio
import re
import time
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PIL import Image

from fastapi import HTTPException

from libreoffice import RenderedShape
from schemas import SpreadsheetMode
from settings import settings

processing = pytest.importorskip(
    "processing", reason="PaddleOCR (processing.py) not importable"
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


# ---------------------------------------------------------------------------
#  Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    """Keep temp files and artifacts inside the test's tmp_path."""
    monkeypatch.setattr(settings, "temp_dir", str(tmp_path))
    monkeypatch.setattr(settings, "save_failed_artifacts", False)
    monkeypatch.setattr(settings, "save_table_conversion_artifacts", False)


@pytest.fixture
def request_stub():
    """A stand-in for fastapi.Request carrying only the app.state we touch."""
    lo_server = MagicMock()
    # Drawing rendering fails like a missing LibreOffice would; tests that need
    # renderings install a fake (see `fake_render`).
    lo_server.render_drawings.side_effect = RuntimeError("no LibreOffice in unit tests")
    state = types.SimpleNamespace(
        pipeline_wrapper=MagicMock(),
        excel_sem=asyncio.Semaphore(1),
        libreoffice_sem=asyncio.Semaphore(1),
        # The PDF conversion finds no output file and fails, so reaching it is observable.
        libreoffice_server=lo_server,
        img_desc_sem={},
    )
    return types.SimpleNamespace(app=types.SimpleNamespace(state=state))


def _process(
    request_stub,
    fixture: str,
    mode: SpreadsheetMode,
    model: str | None = None,
    client=None,
    **kwargs,
):
    content = (FIXTURES / fixture).read_bytes()
    return asyncio.run(
        processing.process_document(
            request_stub,
            content,
            model,
            client,
            time.perf_counter(),
            spreadsheet_mode=mode,
            **kwargs,
        )
    )


def _toc(md: str) -> list[tuple[str, int]]:
    """(label, line) rows of the table of contents, header region only."""
    lines = md.splitlines()
    start = lines.index("## Table of contents")
    end = lines.index("---", start)
    rows = []
    for line in lines[start:end]:
        m = re.match(r"^\|\s*(.+?)\s*\|\s*(\d+)\s*\|$", line.strip())
        if m and m.group(1) != "Section":
            rows.append((m.group(1), int(m.group(2))))
    return rows


# ---------------------------------------------------------------------------
#  Successful pandas conversions
# ---------------------------------------------------------------------------


class TestPandasOutcome:
    def test_auto_returns_a_headed_document_with_an_exact_toc(self, request_stub):
        result = _process(
            request_stub, "multi_table_independent.xlsx", SpreadsheetMode.AUTO
        )
        md = result.page_content
        assert result.mime_ext == ".xlsx"
        assert result.images == {}, "this workbook has no picture or chart"
        assert result.metadata.img_desc_time == 0
        assert "**pandas**" in md, "the header must state the method actually used"

        lines = md.splitlines()
        rows = _toc(md)
        assert [label for label, _ in rows] == ["Employees", "Products", "Orders"]
        for label, line in rows:
            assert lines[line - 1] == f"## {label}"

    def test_explicit_pandas_matches_auto(self, request_stub):
        auto = _process(
            request_stub, "multi_table_independent.xlsx", SpreadsheetMode.AUTO
        )
        pandas = _process(
            request_stub, "multi_table_independent.xlsx", SpreadsheetMode.PANDAS
        )
        assert pandas.page_content == auto.page_content, (
            "no fallback triggers here, so both must agree"
        )


# ---------------------------------------------------------------------------
#  Decision table around an empty spreadsheet
# ---------------------------------------------------------------------------


class TestEmptySpreadsheet:
    def test_pandas_never_falls_back(self, request_stub, monkeypatch):
        """Regression: an explicit mode is the caller's decision, so `pandas` must
        return 422 even when the config enables the PDF+OCR fallback."""
        monkeypatch.setattr(settings, "excel_pdf_fallback_enabled", True)
        with pytest.raises(HTTPException) as exc:
            _process(request_stub, "all_sheets_empty.xlsx", SpreadsheetMode.PANDAS)
        assert exc.value.status_code == 422
        assert "'pandas'" in exc.value.detail

    def test_auto_falls_back_when_enabled(self, request_stub, monkeypatch):
        """`auto` must reach the PDF conversion — the mocked LibreOffice fails there."""
        monkeypatch.setattr(settings, "excel_pdf_fallback_enabled", True)
        with pytest.raises(HTTPException) as exc:
            _process(request_stub, "all_sheets_empty.xlsx", SpreadsheetMode.AUTO)
        assert exc.value.status_code == 500
        assert "PDF conversion error" in exc.value.detail

    def test_auto_returns_422_when_fallback_disabled(self, request_stub, monkeypatch):
        monkeypatch.setattr(settings, "excel_pdf_fallback_enabled", False)
        with pytest.raises(HTTPException) as exc:
            _process(request_stub, "all_sheets_empty.xlsx", SpreadsheetMode.AUTO)
        assert exc.value.status_code == 422


# ---------------------------------------------------------------------------
#  Mode wiring
# ---------------------------------------------------------------------------


class TestModeWiring:
    def test_ocr_mode_never_runs_the_pandas_conversion(self, request_stub, monkeypatch):
        def _boom(*args, **kwargs):
            raise AssertionError("excel2txt must not run in 'ocr' mode")

        monkeypatch.setattr(processing, "excel2txt", _boom)
        with pytest.raises(HTTPException) as exc:
            _process(request_stub, "multi_table_independent.xlsx", SpreadsheetMode.OCR)
        # Straight to the (mocked, failing) LibreOffice conversion.
        assert exc.value.status_code == 500
        assert "PDF conversion error" in exc.value.detail

    def test_both_keeps_going_when_pandas_finds_nothing(
        self, request_stub, monkeypatch
    ):
        """An empty pandas section must not fail a `both` request."""
        monkeypatch.setattr(settings, "excel_pdf_fallback_enabled", False)
        with pytest.raises(HTTPException) as exc:
            _process(request_stub, "all_sheets_empty.xlsx", SpreadsheetMode.BOTH)
        assert exc.value.status_code == 500, "should have reached the OCR conversion"
        assert "PDF conversion error" in exc.value.detail


# ---------------------------------------------------------------------------
#  Pictures and charts in the pandas section
# ---------------------------------------------------------------------------

# A cell range as Excel or ODF writes it — must never reach the Markdown.
_CELL_RANGE_RE = re.compile(
    r"\$?[A-Z]{1,3}\$?\d+:\$?[A-Z]{1,3}\$?\d+|!\$?[A-Z]{1,3}\$?\d+"
)


def _section(md: str, sheet: str) -> str:
    """Body under `## <sheet>`, up to the next heading."""
    lines = md.splitlines()
    start = lines.index(f"## {sheet}")
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )
    return "\n".join(lines[start + 1 : end])


_NAME_RE = re.compile(r"^imgs/[0-9a-f]{16}\.jpg$")


def _srcs(md: str) -> list[str]:
    return re.findall(r'<img src="([^"]+)"', md)


def _install_render(request_stub, colors: list[tuple[int, int, int]]) -> list:
    """Fake LibreOffice rendering of the two charts of drawings.xlsx, as real PNGs."""
    calls = []

    def _render(file_path, out_dir, charts, pictures, output_xlsx=None):
        calls.append((charts, pictures))
        out_dir.mkdir(parents=True, exist_ok=True)
        shapes = []
        for index, (name, color) in enumerate(zip(["Chart 1", "Chart 2"], colors)):
            png = out_dir / f"chart{index}.png"
            Image.new("RGB", (400, 200), color).save(png)
            shapes.append(RenderedShape("Data", "chart", index, name, png))
        return shapes

    request_stub.app.state.libreoffice_server.render_drawings.side_effect = _render
    return calls


@pytest.fixture
def fake_render(request_stub):
    """Two charts rendering to different images."""
    return _install_render(request_stub, [(0, 100, 0), (100, 0, 0)])


@pytest.fixture
def fake_vlm(monkeypatch):
    """Replaces the VLM fan-out; records the context of every image sent to it."""
    sent: list[tuple[str, str]] = []

    async def _describe(client, model_name, imgs_b64, ocrs, semaphore):
        described = {name: ocrs[name] for name in imgs_b64 if name in ocrs}
        sent.extend(described.items())
        return {name: f"Description of {name}." for name in described}, 3.0

    monkeypatch.setattr(processing, "describe_images", _describe)
    return sent


class TestPicturesAndCharts:
    def test_objects_are_listed_under_their_sheet(self, request_stub):
        """LibreOffice unavailable: pictures still come out, charts as summaries."""
        result = _process(request_stub, "drawings.xlsx", SpreadsheetMode.PANDAS)
        md = result.page_content

        assert len(result.images) == 1, "the 32x32 icon is below small_image_threshold"
        (picture,) = result.images
        assert _NAME_RE.match(picture), "images are named after their content"
        data = _section(md, "Data")
        assert data.index("|Month|") < data.index("<chart>"), (
            "table first, then objects"
        )
        assert data.index('"Revenue 2025"') < data.index('"Cost trend"')
        assert "<img" not in data, "charts were not rendered"
        assert _srcs(_section(md, "Pics")) == [picture]

        rows = _toc(md)
        assert [label for label, _ in rows] == ["Data", "Pics"]
        lines = md.splitlines()
        for label, line in rows:
            assert lines[line - 1] == f"## {label}"
        assert not _CELL_RANGE_RE.search(md), "no cell reference in the output"

    def test_small_images_are_referenced_when_not_ignored(
        self, request_stub, fake_vlm, monkeypatch
    ):
        monkeypatch.setattr(settings, "ignore_small_images", False)
        result = _process(
            request_stub,
            "drawings.xlsx",
            SpreadsheetMode.PANDAS,
            model="fake-vlm",
            client=MagicMock(),
        )
        assert len(result.images) == 2, "the picture and the 32x32 icon"
        described = {name for name, _ in fake_vlm}
        (icon,) = set(result.images) - described
        assert len(described) == 1, "the icon is never sent to the VLM"

        md = result.page_content
        assert icon in _srcs(md), "the icon is referenced"
        assert f'<figure>\n<img src="{icon}"' not in md, "bare <img>, no <figure>"
        assert f"Description of {icon}." not in md

    def test_rendered_charts_are_described_with_their_summary(
        self, request_stub, fake_render, fake_vlm
    ):
        result = _process(
            request_stub,
            "drawings.xlsx",
            SpreadsheetMode.PANDAS,
            model="fake-vlm",
            client=MagicMock(),
        )
        assert fake_render == [(True, False)], "charts only: no metafile in this file"
        md = result.page_content
        data, pics = _section(md, "Data"), _section(md, "Pics")
        chart_1, chart_2 = _srcs(data)
        (picture,) = _srcs(pics)
        assert set(result.images) == {chart_1, chart_2, picture}

        contexts = dict(fake_vlm)
        assert len(fake_vlm) == 3, "one VLM call per distinct image"
        assert contexts[chart_1].startswith('Column chart "Revenue 2025"')
        assert contexts[picture] == "", "no meaningful alt text"
        assert result.metadata.img_desc_time == 3

        assert f"Description of {chart_1}." in data
        assert data.index(f'src="{chart_1}"') < data.index("<figcaption>")
        assert f"Description of {picture}." in pics

    def test_identical_images_are_stored_and_described_once(
        self, request_stub, fake_vlm
    ):
        _install_render(request_stub, [(0, 100, 0), (0, 100, 0)])
        result = _process(
            request_stub,
            "drawings.xlsx",
            SpreadsheetMode.PANDAS,
            model="fake-vlm",
            client=MagicMock(),
        )
        chart_1, chart_2 = _srcs(_section(result.page_content, "Data"))
        assert chart_1 == chart_2, "same pixels, same name"
        assert len(result.images) == 2, "the chart image and the picture"
        assert [name for name, _ in fake_vlm].count(chart_1) == 1

    def test_image_cap(self, request_stub, fake_render, monkeypatch):
        monkeypatch.setattr(settings, "excel_max_images", 0)
        result = _process(request_stub, "drawings.xlsx", SpreadsheetMode.PANDAS)
        assert result.images == {}
        md = result.page_content
        assert _section(md, "Data").count("<chart>") == 2, "charts keep their summary"
        assert "> 2 more image(s)" in _section(md, "Data")
        assert "> 2 more image(s)" in _section(md, "Pics")

    def test_extraction_can_be_disabled(self, request_stub, monkeypatch):
        monkeypatch.setattr(settings, "excel_extract_images", False)
        result = _process(request_stub, "drawings.xlsx", SpreadsheetMode.PANDAS)
        assert result.images == {}
        assert "<figure>" not in result.page_content
        assert "<chart>" not in result.page_content
        # Without its pictures, Pics is a lone header cell: pandas renders no table.
        assert [label for label, _ in _toc(result.page_content)] == ["Data"]

    def test_a_workbook_of_pictures_is_not_empty(self, request_stub, monkeypatch):
        monkeypatch.setattr(settings, "excel_pdf_fallback_enabled", True)
        for mode in (SpreadsheetMode.PANDAS, SpreadsheetMode.AUTO):
            result = _process(request_stub, "pictures_only.xlsx", mode)
            assert "**pandas**" in result.page_content
            assert _srcs(_section(result.page_content, "Photos")) == list(result.images)
            assert len(result.images) == 1

    def test_auto_keeps_pandas_for_a_small_table_next_to_a_big_picture(
        self, request_stub, monkeypatch
    ):
        """sparse_image_only.xlsx: ~720 kB of picture, a 3-row table."""
        monkeypatch.setattr(settings, "excel_pdf_fallback_enabled", True)
        result = _process(request_stub, "sparse_image_only.xlsx", SpreadsheetMode.AUTO)
        md = result.page_content
        assert "**pandas**" in md
        assert "|Quarter|Revenue|" in md.replace(" ", "")
        assert len(result.images) == 1 and _srcs(md) == list(result.images)

    def test_auto_still_falls_back_on_content_it_cannot_extract(
        self, request_stub, monkeypatch
    ):
        """sparse_ole_object.xlsx: the weight is an embedded object, not a picture."""
        monkeypatch.setattr(settings, "excel_pdf_fallback_enabled", True)
        with pytest.raises(HTTPException) as exc:
            _process(request_stub, "sparse_ole_object.xlsx", SpreadsheetMode.AUTO)
        assert exc.value.status_code == 500
        assert "PDF conversion error" in exc.value.detail

    def test_a_fallback_extracts_nothing(self, request_stub, monkeypatch):
        """Once `auto` has chosen OCR, no picture is decoded and nothing is rendered."""
        monkeypatch.setattr(settings, "excel_pdf_fallback_enabled", True)
        monkeypatch.setattr(settings, "excel_min_input_for_fallback_mb", 0)
        calls = []
        monkeypatch.setattr(processing, "decode_objects", lambda **kw: calls.append(kw))
        monkeypatch.setattr(
            processing, "render_with_libreoffice", lambda **kw: calls.append(kw)
        )
        with pytest.raises(HTTPException) as exc:
            _process(
                request_stub,
                "drawings.xlsx",
                SpreadsheetMode.AUTO,
                excel_min_output_ratio=1000.0,
            )
        assert "PDF conversion error" in exc.value.detail, "should have fallen back"
        assert calls == []


# ---------------------------------------------------------------------------
#  Size budget at the route level
# ---------------------------------------------------------------------------


class TestSizeBudget:
    def test_413_when_the_only_section_is_oversized(self, request_stub, monkeypatch):
        monkeypatch.setattr(settings, "excel_max_output_ratio", 0.0001)
        with pytest.raises(HTTPException) as exc:
            _process(
                request_stub, "multi_table_independent.xlsx", SpreadsheetMode.PANDAS
            )
        assert exc.value.status_code == 413
        assert "pandas" in exc.value.detail
