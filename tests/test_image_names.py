"""Content-addressed image names: `imgs/<sha256[:16]>.jpg`.

An extracted image is named after the JPEG bytes the server returns, so identical
images collapse into one entry (one file in the archive, one VLM call) and two
different images can never share a name — which PaddleX's box-based names do when
two pages hold a picture at the same place.
"""

import asyncio
import hashlib
import io
import re
import time
import types
from unittest.mock import MagicMock

import pytest
from PIL import Image

from postprocessing import name_by_content
from settings import settings
from utils import EncodedImage

_NAME_RE = re.compile(r"^imgs/[0-9a-f]{16}\.jpg$")


def _image(
    color: tuple[int, int, int], size: tuple[int, int] = (100, 80)
) -> Image.Image:
    return Image.new("RGB", size, color)


def _div(name: str, ocr: str) -> str:
    """An image block as PaddleOCR-VL writes it."""
    return (
        f'<div style="text-align: center;"><img src="{name}" alt="Image" '
        f'width="50%" />{ocr}</div>'
    )


# ---------------------------------------------------------------------------
#  EncodedImage
# ---------------------------------------------------------------------------


class TestEncodedImage:
    def test_name_is_a_prefix_of_the_served_file_sha256(self):
        image = EncodedImage.from_pil(_image((200, 30, 30)))
        assert _NAME_RE.match(image.name)
        assert image.name == f"imgs/{hashlib.sha256(image.jpeg).hexdigest()[:16]}.jpg"
        assert image.b64 and (image.width, image.height) == (100, 80)
        with Image.open(io.BytesIO(image.jpeg)) as decoded:
            assert decoded.format == "JPEG"

    def test_same_pixels_same_name_different_pixels_different_name(self):
        red_1 = EncodedImage.from_pil(_image((200, 30, 30)))
        red_2 = EncodedImage.from_pil(_image((200, 30, 30)))
        blue = EncodedImage.from_pil(_image((30, 30, 200)))
        assert red_1.name == red_2.name != blue.name

    def test_non_rgb_modes_are_encoded(self):
        rgba = Image.new("RGBA", (10, 10), (255, 0, 0, 128))
        assert _NAME_RE.match(EncodedImage.from_pil(rgba).name)


# ---------------------------------------------------------------------------
#  Paddle crops, renamed page by page
# ---------------------------------------------------------------------------


class TestNameByContent:
    def test_references_are_rewritten(self):
        old = "imgs/img_in_image_box_10_10_110_90.jpg"
        md, images = name_by_content(_div(old, "ACME"), {old: _image((200, 30, 30))})
        (new,) = images
        assert _NAME_RE.match(new)
        assert f'src="{new}"' in md and old not in md

    def test_a_name_contained_in_another_is_left_alone(self):
        short = "imgs/img_in_image_box_1_2_3_4.jpg"
        longer = "imgs/img_in_image_box_1_2_3_4.jpg.bak"  # contains `short`
        md = _div(short, "a") + _div(longer, "b")
        md, images = name_by_content(md, {short: _image((200, 30, 30))})
        (new,) = images
        assert f'src="{new}"' in md and f'src="{longer}"' in md

    def test_pages_no_longer_collide_and_duplicates_collapse(self):
        """Same box on two pages, different pictures; same picture at two boxes."""
        box_a = "imgs/img_in_image_box_10_10_110_90.jpg"
        box_b = "imgs/img_in_image_box_200_10_300_90.jpg"
        page_1, imgs_1 = name_by_content(
            _div(box_a, "logo"), {box_a: _image((200, 30, 30))}
        )
        page_2, imgs_2 = name_by_content(
            _div(box_a, "photo") + _div(box_b, "logo"),
            {box_a: _image((30, 30, 200)), box_b: _image((200, 30, 30))},
        )
        merged = imgs_1 | imgs_2  # what the worker returns
        refs_1 = re.findall(r'src="([^"]+)"', page_1)
        refs_2 = re.findall(r'src="([^"]+)"', page_2)
        assert len(merged) == 2, "red logo once, blue photo once"
        assert refs_1[0] == refs_2[1], "the logo keeps one name on both pages"
        assert refs_1[0] != refs_2[0], "same box, different picture: different names"


# ---------------------------------------------------------------------------
#  Through process_document (PaddleOCR worker faked)
# ---------------------------------------------------------------------------

processing = pytest.importorskip(
    "processing", reason="PaddleOCR (processing.py) not importable"
)


@pytest.fixture
def request_stub(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "temp_dir", str(tmp_path))
    monkeypatch.setattr(settings, "save_failed_artifacts", False)

    box_a = "imgs/img_in_image_box_10_10_110_90.jpg"
    box_b = "imgs/img_in_image_box_200_10_300_90.jpg"

    async def _run(file_path, use_ocr_for_image_block=True):
        # What `_worker_predict` produces for a 2-page document: page 1 has the red
        # logo at box A; page 2 has a blue photo at box A and the logo at box B.
        page_1, imgs_1 = name_by_content(
            f"# One\n\n{_div(box_a, 'ACME')}", {box_a: _image((200, 30, 30))}
        )
        page_2, imgs_2 = name_by_content(
            f"# Two\n\n{_div(box_a, 'photo')}\n\n{_div(box_b, 'ACME')}",
            {box_a: _image((30, 30, 200)), box_b: _image((200, 30, 30))},
        )
        return [page_1, page_2], imgs_1 | imgs_2, 0.0

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


def test_each_distinct_image_is_returned_and_described_once(request_stub, monkeypatch):
    sent: list[tuple[str, str]] = []

    async def _describe(client, model_name, imgs_b64, ocrs, semaphore):
        described = {name: ocrs[name] for name in imgs_b64 if name in ocrs}
        sent.extend(described.items())
        return {name: f"Description of {name}." for name in described}, 1.0

    monkeypatch.setattr(processing, "describe_images", _describe)
    buf = io.BytesIO()
    _image((255, 255, 255), (300, 200)).save(buf, "PNG")

    result = asyncio.run(
        processing.process_document(
            request_stub, buf.getvalue(), "fake-vlm", MagicMock(), time.perf_counter()
        )
    )
    md = result.page_content
    refs = re.findall(r'<img src="([^"]+)"', md)
    logo, photo, logo_again = refs
    assert logo == logo_again != photo
    assert set(result.images) == {logo, photo}
    assert sorted(name for name, _ in sent) == sorted([logo, photo]), (
        "one VLM call per distinct image"
    )
    assert dict(sent)[photo] == "photo", "the photo keeps its own OCR context"
    assert md.count(f"Description of {logo}.") == 2
    assert md.count(f"Description of {photo}.") == 1
