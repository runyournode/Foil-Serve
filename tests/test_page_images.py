"""Pages handed to the pipeline are rendered by foil (page_images.py).

- images keep their native pixels: no image → PDF (72 dpi) → render ×2 round trip
- every frame of a multipage TIFF is a page, WebP is read
- a page above the pixel budget is rendered / downscaled to it, never refused
- documents longer than `pipeline_chunk_pages` go to the pool in page ranges, so a
  small document is served between two ranges of a big one
"""

from pathlib import Path

import numpy as np
import pypdfium2 as pdfium
import pytest
from PIL import Image

from page_images import PDF_RENDER_SCALE, iter_pages, page_count


def _pages(path: Path, max_mp: float = 0) -> list[np.ndarray]:
    return list(iter_pages(path, 0, page_count(path), max_mp))


def _blank_pdf(path: Path, *sizes_pt: tuple[float, float]) -> Path:
    pdf = pdfium.PdfDocument.new()
    for w, h in sizes_pt:
        pdf.new_page(w, h)
    pdf.save(path)
    pdf.close()
    return path


def test_tiff_keeps_native_pixels(tmp_path):
    path = tmp_path / "scan.tiff"
    Image.new("L", (600, 800), 200).save(path, dpi=(300, 300))
    (page,) = _pages(path)
    assert page.shape == (800, 600, 3)  # was 4x the pixels through a 72 dpi PDF


def test_multipage_tiff_one_page_per_frame(tmp_path):
    path = tmp_path / "multi.tiff"
    colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
    frames = [Image.new("RGB", (50, 40), c) for c in colors]
    frames[0].save(path, save_all=True, append_images=frames[1:])
    assert page_count(path) == 3
    second, third = iter_pages(path, 1, 3, 0)
    # BGR, the PaddleX convention
    assert tuple(second[0, 0]) == (0, 255, 0)
    assert tuple(third[0, 0]) == (255, 0, 0)


def test_webp_is_read(tmp_path):
    path = tmp_path / "img.webp"
    Image.new("RGB", (64, 32), (10, 20, 30)).save(path, lossless=True)
    (page,) = _pages(path)
    assert page.shape == (32, 64, 3)
    assert tuple(page[0, 0]) == (30, 20, 10)


def test_transparent_png_has_white_background(tmp_path):
    path = tmp_path / "alpha.png"
    Image.new("RGBA", (10, 10), (0, 0, 0, 0)).save(path)
    (page,) = _pages(path)
    assert tuple(page[0, 0]) == (255, 255, 255)


@pytest.mark.parametrize("fmt", ["PNG", "JPEG"])
def test_large_image_downscaled_to_budget(tmp_path, fmt):
    path = tmp_path / f"big.{fmt.lower()}"
    Image.new("RGB", (4000, 3000), (120, 130, 140)).save(path, fmt)
    (page,) = _pages(path, max_mp=1)
    h, w, _ = page.shape
    assert h * w <= 1_000_000
    assert h * w > 900_000  # downscaled to the budget, not far below
    assert abs(w / h - 4 / 3) < 0.01


def test_huge_image_header_is_not_refused(tmp_path):
    # 210 Mpx: above Pillow's default refusal threshold (179 Mpx)
    path = tmp_path / "huge.png"
    Image.new("1", (15000, 14000)).save(path)
    assert page_count(path) == 1  # opened without DecompressionBombError


def test_pdf_rendered_like_paddlex(tmp_path):
    path = _blank_pdf(tmp_path / "a4.pdf", (595, 842))
    (page,) = _pages(path, max_mp=16)
    assert page.shape == (
        round(842 * PDF_RENDER_SCALE),
        round(595 * PDF_RENDER_SCALE),
        3,
    )


def test_giant_pdf_page_capped_per_page(tmp_path):
    path = _blank_pdf(tmp_path / "poster.pdf", (595, 842), (10_000, 10_000))
    normal, poster = _pages(path, max_mp=4)
    assert normal.shape[:2] == (1684, 1190)  # under budget: untouched
    assert poster.shape[0] * poster.shape[1] <= 4_000_000 * 1.01
    assert poster.shape[0] * poster.shape[1] > 3_900_000


def test_pdf_page_range(tmp_path):
    path = _blank_pdf(tmp_path / "doc.pdf", *[(100 + i, 100) for i in range(5)])
    assert page_count(path) == 5
    widths = [p.shape[1] for p in iter_pages(path, 2, 4, 0)]
    assert widths == [round(102 * PDF_RENDER_SCALE), round(103 * PDF_RENDER_SCALE)]
