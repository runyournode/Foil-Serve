"""Page images handed to the PaddleOCR pipeline, rendered by foil itself.

PaddleX can read PDFs and a few image formats on its own, but then it renders every
PDF page at one fixed scale and reads only the first frame of a TIFF and no WebP at
all. foil therefore turns every input into BGR arrays (the PaddleX convention), one
per page, with a pixel budget per page:

- PDF: rendered with pypdfium2 exactly as PaddleX does (same call, forms included),
  at `PDF_RENDER_SCALE` — lowered for the pages that would exceed the budget (posters,
  plans).
- Image: every frame decoded once by Pillow (multipage TIFF, WebP, …), EXIF
  orientation applied, downscaled only above the budget. No image → PDF → image round
  trip, no resampling of what is already a raster.

Pure module (pypdfium2, Pillow, numpy): no Paddle, usable from the API process.
"""

import math
import threading
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pypdfium2 as pdfium
from PIL import Image, ImageOps

# PaddleX's own default (PADDLE_PDX_PDF_RENDER_SCALE): 2.0 = 144 dpi. Kept so that the
# output of ordinary documents does not change.
PDF_RENDER_SCALE = 2.0

# Pillow refuses images above 2 x MAX_IMAGE_PIXELS when it opens them. Below that, an
# image is always downscaled, never refused: the guard only stops decompression bombs
# (a 1 MB PNG declaring 50000 x 50000). An A0 page scanned at 600 dpi is 558 Mpx.
Image.MAX_IMAGE_PIXELS = 500_000_000

# pdfium is not thread-safe; the API process counts pages from several threads.
_PDFIUM_LOCK = threading.Lock()


def is_pdf(path: Path) -> bool:
    """PDF signature in the first KB (the format tolerates leading garbage)."""
    with path.open("rb") as f:
        return b"%PDF-" in f.read(1024)


def page_count(path: Path) -> int:
    """Number of pages (PDF) or frames (image) of `path`."""
    if is_pdf(path):
        with _PDFIUM_LOCK:
            pdf = pdfium.PdfDocument(path)
            try:
                return len(pdf)
            finally:
                pdf.close()
    with Image.open(path) as img:
        return getattr(img, "n_frames", 1)


def _budget_factor(width: float, height: float, max_pixels: int) -> float:
    """Linear factor (<= 1) that brings width x height under `max_pixels`."""
    if max_pixels <= 0 or width * height <= max_pixels:
        return 1.0
    return math.sqrt(max_pixels / (width * height))


def iter_pages(
    path: Path, start: int, stop: int, max_megapixels: float
) -> Iterator[np.ndarray]:
    """Yield pages [start, stop) of `path` as BGR uint8 arrays, at most
    `max_megapixels` each (0 = no cap)."""
    max_pixels = int(max_megapixels * 1_000_000)
    if is_pdf(path):
        yield from _iter_pdf_pages(path, start, stop, max_pixels)
    else:
        yield from _iter_image_frames(path, start, stop, max_pixels)


def _iter_pdf_pages(
    path: Path, start: int, stop: int, max_pixels: int
) -> Iterator[np.ndarray]:
    with _PDFIUM_LOCK:
        pdf = pdfium.PdfDocument(path)
        pdf.init_forms()  # as PaddleX: form fields are rendered
    try:
        for index in range(start, min(stop, len(pdf))):
            # The lock is held per page only, never across a yield.
            with _PDFIUM_LOCK:
                page = pdf[index]
                w_pt, h_pt = page.get_size()
                scale = PDF_RENDER_SCALE * _budget_factor(
                    w_pt * PDF_RENDER_SCALE, h_pt * PDF_RENDER_SCALE, max_pixels
                )
                # pypdfium2 renders BGR, the channel order PaddleX expects
                array = page.render(scale=scale).to_numpy()
                page.close()
            yield array
    finally:
        with _PDFIUM_LOCK:
            pdf.close()


def _to_rgb(img: Image.Image) -> Image.Image:
    """RGB copy; transparent areas become white (a page background), not black."""
    if img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info:
        rgba = img.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(background, rgba).convert("RGB")
    return img.convert("RGB")


def _iter_image_frames(
    path: Path, start: int, stop: int, max_pixels: int
) -> Iterator[np.ndarray]:
    with Image.open(path) as img:
        frames = getattr(img, "n_frames", 1)
        for index in range(start, min(stop, frames)):
            img.seek(index)
            factor = _budget_factor(img.width, img.height, max_pixels)
            if factor < 1.0 and frames == 1 and img.format == "JPEG":
                # Decode at a reduced DCT scale: the full-size bitmap never exists.
                img.draft(
                    "RGB",
                    (math.ceil(img.width * factor), math.ceil(img.height * factor)),
                )
            frame = _to_rgb(ImageOps.exif_transpose(img))
            factor = _budget_factor(frame.width, frame.height, max_pixels)
            if factor < 1.0:
                frame = frame.resize(
                    (
                        max(1, int(frame.width * factor)),
                        max(1, int(frame.height * factor)),
                    ),
                    Image.Resampling.LANCZOS,
                    reducing_gap=3.0,
                )
            yield np.ascontiguousarray(np.asarray(frame)[:, :, ::-1])  # RGB → BGR
