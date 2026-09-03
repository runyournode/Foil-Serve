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

from fastapi import HTTPException

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
    state = types.SimpleNamespace(
        pipeline_wrapper=MagicMock(),
        excel_sem=asyncio.Semaphore(1),
        libreoffice_sem=asyncio.Semaphore(1),
        # Raises on any conversion attempt, so reaching LibreOffice is observable.
        libreoffice_server=MagicMock(),
    )
    return types.SimpleNamespace(app=types.SimpleNamespace(state=state))


def _process(request_stub, fixture: str, mode: SpreadsheetMode, **kwargs):
    content = (FIXTURES / fixture).read_bytes()
    return asyncio.run(
        processing.process_document(
            request_stub,
            content,
            None,
            None,
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
        assert result.images == {}, "the pandas path extracts no images"
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
