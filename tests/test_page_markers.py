"""`add_pdf_page`: `<page number="n/n_max" />` at the start of each page.

Only paginated inputs are marked (PDF, office documents converted to PDF,
multipage TIFF/WebP); spreadsheets and single images never are. Empty pages are
kept by the pipeline so that page n of the output is page n of the source.
"""

import asyncio
import io
import time
import types
from unittest.mock import MagicMock

import pytest
from PIL import Image

from postprocessing import join_marked_pages
from settings import settings


class TestJoinMarkedPages:
    def test_each_page_starts_with_its_marker(self):
        md = join_marked_pages(["# One", "Two"])
        assert md == '<page number="1/2" />\n\n# One\n\n<page number="2/2" />\n\nTwo'

    def test_no_blank_line_at_page_edges(self):
        md = join_marked_pages(["\n\nCHUBB  \n\n", "  \nNext"])
        assert md == '<page number="1/2" />\n\nCHUBB\n\n<page number="2/2" />\n\nNext'

    def test_empty_page_keeps_its_marker_and_numbering(self):
        md = join_marked_pages(["a", "  \n", "c"])
        assert md == (
            '<page number="1/3" />\n\na\n\n'
            '<page number="2/3" />\n\n'
            '<page number="3/3" />\n\nc'
        )


# ---------------------------------------------------------------------------
#  Through process_document (PaddleOCR worker faked)
# ---------------------------------------------------------------------------

processing = pytest.importorskip(
    "processing", reason="PaddleOCR (processing.py) not importable"
)

# What `_worker_predict` returns for a 3-page document whose page 2 is blank
PAGES = ["# Title\n\nFirst page.", "", "Last page."]


@pytest.fixture
def request_stub(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "temp_dir", str(tmp_path))
    monkeypatch.setattr(settings, "save_failed_artifacts", False)

    async def _run(file_path, use_ocr_for_image_block=True):
        return list(PAGES), {}, 0.0

    wrapper = MagicMock()
    wrapper.run = _run
    state = types.SimpleNamespace(
        pipeline_wrapper=wrapper,
        excel_sem=asyncio.Semaphore(1),
        libreoffice_sem=asyncio.Semaphore(1),
        libreoffice_server=MagicMock(),
        img_desc_sem={},
    )
    return types.SimpleNamespace(app=types.SimpleNamespace(state=state))


def _file(fmt: str) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (300, 200), (255, 255, 255)).save(buf, fmt)
    return buf.getvalue()


def _convert(request_stub, content: bytes) -> str:
    result = asyncio.run(
        processing.process_document(
            request_stub, content, None, None, time.perf_counter()
        )
    )
    return result.page_content


def test_pdf_pages_are_marked(request_stub, monkeypatch):
    monkeypatch.setattr(settings, "add_pdf_page", True)
    md = _convert(request_stub, _file("PDF"))
    assert md == (
        '<page number="1/3" />\n\n# Title\n\nFirst page.\n\n'
        '<page number="2/3" />\n\n'
        '<page number="3/3" />\n\nLast page.'
    )


def test_disabled(request_stub, monkeypatch):
    monkeypatch.setattr(settings, "add_pdf_page", False)
    assert "<page" not in _convert(request_stub, _file("PDF"))


def test_single_image_is_not_marked(request_stub, monkeypatch):
    monkeypatch.setattr(settings, "add_pdf_page", True)
    assert "<page" not in _convert(request_stub, _file("PNG"))
