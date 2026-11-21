"""A document in which the pipeline finds nothing (a photo, a drawing without text).

With an image description requested, its whole pages go to the VLM as figures
(`empty_document_vlm_max_pages`); without one, or beyond that page count, the
answer stays 422 "no content".
"""

import asyncio
import contextlib
import io
import re
import time
import types
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from PIL import Image

from settings import settings

processing = pytest.importorskip(
    "processing", reason="PaddleOCR (processing.py) not importable"
)


class _FakeLibreOfficePool:
    def __init__(self, server) -> None:
        self.server = server

    @contextlib.asynccontextmanager
    async def acquire(self):
        yield self.server


@pytest.fixture
def request_stub(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "temp_dir", str(tmp_path))
    monkeypatch.setattr(settings, "save_failed_artifacts", False)
    monkeypatch.setattr(settings, "empty_document_vlm_max_pages", 5)

    async def _run(file_path, use_ocr_for_image_block=True):
        from page_images import page_count
        from pathlib import Path

        return [""] * page_count(Path(file_path)), {}, 0.0  # nothing detected

    wrapper = MagicMock()
    wrapper.run = _run
    state = types.SimpleNamespace(
        pipeline_wrapper=wrapper,
        excel_sem=asyncio.Semaphore(1),
        libreoffice_pool=_FakeLibreOfficePool(MagicMock()),
        img_desc_sem={},
        vlm_description_cache=None,
    )
    return types.SimpleNamespace(app=types.SimpleNamespace(state=state))


@pytest.fixture
def described(monkeypatch) -> list[tuple[str, str]]:
    sent: list[tuple[str, str]] = []

    async def _describe(client, model_name, imgs_b64, ocrs, semaphore, cache=None):
        found = {name: ocrs[name] for name in imgs_b64 if name in ocrs}
        sent.extend(found.items())
        return {name: f"A photo ({name})." for name in found}, 1.0

    monkeypatch.setattr(processing, "describe_images", _describe)
    return sent


def _photo(size=(3000, 2000), frames: int = 1) -> bytes:
    buf = io.BytesIO()
    imgs = [Image.new("RGB", size, (40 * i, 120, 200)) for i in range(frames)]
    imgs[0].save(buf, "TIFF", save_all=True, append_images=imgs[1:])
    return buf.getvalue()


def _convert(request_stub, content: bytes, model: str | None):
    return asyncio.run(
        processing.process_document(
            request_stub,
            content,
            model,
            MagicMock() if model else None,
            time.perf_counter(),
        )
    )


def test_whole_page_is_described(request_stub, described):
    result = _convert(request_stub, _photo(), "fake-vlm")
    (name,) = result.images
    assert re.findall(r'<img src="([^"]+)"', result.page_content) == [name]
    assert f"A photo ({name})." in result.page_content
    assert described == [(name, "")]  # no OCR context: nothing was read


def test_whole_page_is_downscaled_for_the_vlm(request_stub, described):
    result = _convert(request_stub, _photo(size=(5000, 3000)), "fake-vlm")
    (image_b64,) = result.images.values()
    import base64

    with Image.open(io.BytesIO(base64.b64decode(image_b64))) as img:
        assert max(img.size) == 2048


def test_one_figure_per_page_with_markers(request_stub, described, monkeypatch):
    monkeypatch.setattr(settings, "add_pdf_page", True)
    result = _convert(request_stub, _photo(size=(800, 600), frames=3), "fake-vlm")
    md = result.page_content
    assert md.count("<page number=") == 3
    assert len(re.findall(r"<img src=", md)) == 3
    assert len(described) == 3


def test_without_model_stays_422(request_stub):
    with pytest.raises(HTTPException) as exc:
        _convert(request_stub, _photo(), None)
    assert exc.value.status_code == 422


def test_beyond_page_cap_stays_422(request_stub, described, monkeypatch):
    monkeypatch.setattr(settings, "empty_document_vlm_max_pages", 2)
    with pytest.raises(HTTPException) as exc:
        _convert(request_stub, _photo(size=(800, 600), frames=3), "fake-vlm")
    assert exc.value.status_code == 422
    assert not described


def test_disabled_stays_422(request_stub, described, monkeypatch):
    monkeypatch.setattr(settings, "empty_document_vlm_max_pages", 0)
    with pytest.raises(HTTPException) as exc:
        _convert(request_stub, _photo(), "fake-vlm")
    assert exc.value.status_code == 422
