"""Corrupted files of a supported type are the client's fault: HTTP 422, not 500."""

import asyncio
import contextlib
import os
import shutil
import time
import types
import zipfile
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

import pipeline
from libreoffice import LibreOfficeServer, convert_to_pdf
from settings import settings
from utils import UnreadableDocument

processing = pytest.importorskip("processing", reason="processing.py not importable")
needs_soffice = pytest.mark.skipif(
    shutil.which("soffice") is None, reason="LibreOffice not installed"
)


def test_truncated_pdf_is_unreadable(tmp_path):
    path = tmp_path / "bad.pdf"
    path.write_bytes(b"%PDF-1.7\n%garbage\n")
    wrapper = pipeline.PaddlePipelineWrapper.__new__(pipeline.PaddlePipelineWrapper)
    wrapper._pool = MagicMock()  # never reached
    with pytest.raises(UnreadableDocument):
        asyncio.run(wrapper.run(str(path)))


@needs_soffice
@pytest.mark.parametrize("kind", ["random", "invalid_xml"])
def test_corrupted_docx_is_unreadable(tmp_path, kind):
    path = tmp_path / "bad.docx"
    if kind == "random":
        path.write_bytes(b"PK\x03\x04" + os.urandom(5000))
    else:
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("[Content_Types].xml", "<Types/>")
            z.writestr("word/document.xml", b"0" * 100_000)
    srv = LibreOfficeServer(str(tmp_path / "rt"), slot=0)
    srv.start()
    try:
        with pytest.raises(UnreadableDocument):
            convert_to_pdf(path, ".docx", srv)
    finally:
        srv.stop()


class _FakeLibreOfficePool:
    def __init__(self, server) -> None:
        self.server = server

    @contextlib.asynccontextmanager
    async def acquire(self):
        yield self.server


def test_unreadable_document_answers_422(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "temp_dir", str(tmp_path))
    monkeypatch.setattr(settings, "save_failed_artifacts", False)

    async def _run(file_path, use_ocr_for_image_block=True):
        raise UnreadableDocument("cannot read the document: Data format error")

    wrapper = MagicMock()
    wrapper.run = _run
    state = types.SimpleNamespace(
        pipeline_wrapper=wrapper,
        excel_sem=asyncio.Semaphore(1),
        libreoffice_pool=_FakeLibreOfficePool(MagicMock()),
        img_desc_sem={},
        vlm_description_cache=None,
    )
    request = types.SimpleNamespace(app=types.SimpleNamespace(state=state))
    content = b"%PDF-1.7\n%broken\n"
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            processing.process_document(
                request, content, None, None, time.perf_counter()
            )
        )
    assert exc.value.status_code == 422
