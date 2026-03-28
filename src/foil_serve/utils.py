import io
import base64
import logging
import tarfile
import zipfile
from pathlib import Path
from typing import Literal

import chardet
import magic
import zstandard
from PIL.Image import Image, open as pil_open

from schemas import Metadata

logger = logging.getLogger(__name__)


class UnsupportedMimeTypeError(Exception):
    """Raised when the detected MIME type is not in mime_def."""

    def __init__(self, raw_mime: str) -> None:
        super().__init__(f"File type not supported: {raw_mime}")
        self.raw_mime = raw_mime


# -----------------------------------
#  MIME type detection
# -----------------------------------

MimeExt = Literal[
    ".pdf",
    ".docx",
    ".doc",
    ".odt",
    ".xlsx",
    ".xls",
    ".ods",
    ".pptx",
    ".ppt",
    ".odp",
    ".png",
    ".jpg",
    ".bmp",
    ".webp",
    ".tiff",
    ".txt",
    ".json",
    ".csv",
    ".xml",
]

# Maps MIME type strings to file extensions.
# Keys also define which MIME types are accepted for processing.
mime_def: dict[str, MimeExt | None] = {
    # PDF
    "application/pdf": ".pdf",
    # Microsoft Office
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.ms-powerpoint": ".ppt",
    # LibreOffice / OpenDocument
    "application/vnd.oasis.opendocument.text": ".odt",
    "application/vnd.oasis.opendocument.spreadsheet": ".ods",
    "application/vnd.oasis.opendocument.presentation": ".odp",
    # Images — passed directly to the pipeline (except TIFF and WebP: converted to PDF first)
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/bmp": ".bmp",
    "image/webp": ".webp",
    "image/tiff": ".tiff",
    # Plain text — returned as-is without pipeline
    "text/plain": ".txt",
    "application/json": ".json",
    "text/csv": ".csv",
    "text/xml": ".xml",
    "application/xml": ".xml",
    # Empty file
    "inode/x-empty": None,
}


# -----------------------------------
#  Image utilities
# -----------------------------------


def pil_to_b64(img: Image) -> str:
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=95, subsampling=0)
    b64 = base64.b64encode(buffer.getvalue()).decode("utf-8")
    buffer.close()
    return b64


def batch_pil_to_b64(images_dict: dict[str, Image]) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, img in images_dict.items():
        result[name] = pil_to_b64(img)
        img.close()  # immediate release of PIL buffer
    return result


# -----------------------------------
#  Excel conversion
# -----------------------------------


def check_file_size(size_bytes: int, limit_mb: int, label: str) -> None:
    """Raise ValueError if size_bytes exceeds limit_mb."""
    if size_bytes > limit_mb * 1024 * 1024:
        raise ValueError(
            f"{label} too large: {size_bytes / 1024 / 1024:.1f} MB (max {limit_mb} MB)"
        )


def check_zip_uncompressed_size(path: Path, max_bytes: int) -> None:
    """
    Two-phase zip bomb guard for ZIP-based formats (.xlsx, .ods, .docx, .pptx, .odt, .odp).

    Phase 1 — Declared size (instant, no decompression):
        Sum of file_size from the ZIP central directory. If it exceeds max_bytes,
        reject immediately. This catches oversized legitimate files at near-zero cost.

    Phase 2 — Actual decompression (catches forged headers):
        Decompress every entry by chunks. If the real decompressed total exceeds
        1.25× the declared total, raise a zip bomb error. This detects archives
        where the declared size has been forged to bypass Phase 1.
    """
    with zipfile.ZipFile(path) as zf:
        infos = zf.infolist()

        # Phase 1: declared size from central directory
        declared_total = sum(i.file_size for i in infos)
        if declared_total > max_bytes:
            raise ValueError(
                f"ZIP archive ({path.suffix.lower()}) declared uncompressed size "
                f"{declared_total / 1024**2:.1f} MB exceeds "
                f"{max_bytes // 1024**2} MB limit"
            )

        # Phase 2: actual decompression — detect forged declared sizes
        bomb_threshold = int(declared_total * 1.25)
        real_total = 0
        for info in infos:
            with zf.open(info) as f:
                while chunk := f.read(65536):
                    real_total += len(chunk)
                    if real_total > bomb_threshold:
                        raise ValueError(
                            f"ZIP archive ({path.suffix.lower()}) actual decompressed size "
                            f"exceeds 1.25x declared size "
                            f"({declared_total / 1024**2:.1f} MB) — zip bomb detected"
                        )


def image_to_pdf(path: Path, output_dir: Path) -> Path:
    """
    Convert an image file to PDF using Pillow. Supports multi-page formats (e.g. TIFF)
    and formats not natively supported by the Paddle pipeline (e.g. WebP).
    """
    img = pil_open(path)
    pages: list[Image] = []
    try:
        while True:
            pages.append(img.copy().convert("RGB"))
            img.seek(img.tell() + 1)
    except EOFError:
        pass
    finally:
        img.close()
    pdf_path = output_dir / "converted.pdf"
    pages[0].save(pdf_path, save_all=True, append_images=pages[1:])
    for p in pages:
        p.close()
    return pdf_path


# -----------------------------------
#  File preparation
# -----------------------------------


def build_tar_zst(
    page_content: str,
    imgs_b64: dict[str, str],
    metadata: Metadata,
    mime_ext: str,
    raw_mime: str,
) -> bytes:
    """
    Build an in-memory tar.zst archive with the processed document artifacts.

    Archive layout:
        page_content.md     — Markdown output
        metadata.json       — Metadata fields (timing, etc.)
        mime.txt            — Raw MIME type and mapped extension (one per line)
        imgs/<name>         — Extracted images as JPEG files (paths match Markdown references)
    """
    tar_buf = io.BytesIO()

    def _add(_tar: tarfile.TarFile, name: str, data: bytes) -> None:
        info = tarfile.TarInfo(name=name)
        info.size = len(data)
        _tar.addfile(info, io.BytesIO(data))

    with tarfile.open(fileobj=tar_buf, mode="w") as tar:
        _add(tar, "page_content.md", page_content.encode())
        _add(tar, "metadata.json", metadata.model_dump_json(indent=2).encode())
        _add(tar, "mime.txt", f"{raw_mime}\n{mime_ext}\n".encode())
        for img_name, img_b64 in imgs_b64.items():
            # img_name already has the folder name (imgs/...) and will match the Markdown image references: <img src="imgs/...">
            _add(tar, img_name, base64.b64decode(img_b64))

    tar_buf.seek(0)
    return zstandard.ZstdCompressor().compress(tar_buf.read())


def read_text_smart(path: Path) -> str:
    """
    Decode a text file with automatic encoding detection.

    Strategy (in order):
      1. UTF-8 (strict) — covers the vast majority of modern files
      2. chardet detection — handles exotic encodings (Shift-JIS, EUC-JP, Big5, etc.)
      3. Fallback chain: UTF-16 → cp1252 → latin-1
         latin-1 is the last resort as it never raises (every byte maps to U+0000–U+00FF)

    Raises ValueError only when every strategy fails — in practice latin-1 makes that
    impossible, but the explicit raise keeps the type signature honest.
    """
    raw = path.read_bytes()

    # 1. UTF-8 fast path
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass

    # 2. chardet detection
    detected = chardet.detect(raw)
    enc = detected.get("encoding")
    if enc:
        try:
            return raw.decode(enc)
        except Exception:
            pass

    # 3. Fallback chain
    for fallback in ("utf-16", "cp1252", "latin-1"):
        try:
            return raw.decode(fallback)
        except Exception:
            continue

    raise ValueError(f"Unable to decode file: {path.name}")


_OOXML_DIR_PREFIX_MAP: dict[str, str] = {
    "word/": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xl/": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ppt/": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def _detect_ooxml(path: Path) -> str | None:
    """Detect OOXML type from ZIP central directory only (no decompression).

    Checks for [Content_Types].xml (OOXML marker) and known directory
    prefixes (word/, xl/, ppt/) in the ZIP namelist. No entry content is
    read, so this is safe to call before zip bomb checks.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            if "[Content_Types].xml" not in names:
                return None
            for prefix, mime_type in _OOXML_DIR_PREFIX_MAP.items():
                if any(n.startswith(prefix) for n in names):
                    logger.info(
                        "OOXML fallback: detected %s from ZIP namelist",
                        prefix.rstrip("/"),
                    )
                    return mime_type
    except zipfile.BadZipFile:
        return None
    return None


def prepare_input_file(file_content: bytes, tmpdir: Path) -> tuple[Path, MimeExt, str]:
    """
    Write file content to tmpdir, detect MIME type, rename with proper extension.

    Returns (file_path, mime_ext, raw_mime) where:
      - mime_ext is like '.pdf', '.docx', etc. (a MimeExt literal)
      - raw_mime is the raw MIME type string, e.g. 'application/pdf'
    """
    input_file = tmpdir / "input_file.bin"
    input_file.write_bytes(file_content)
    raw_mime: str = magic.from_file(input_file, mime=True)

    # Fallback: some OOXML files (.docx, .xlsx, .pptx) are detected as
    # application/octet-stream by libmagic.  Inspect the ZIP content types
    # to resolve the actual OOXML variant.
    if raw_mime == "application/octet-stream":
        raw_mime = _detect_ooxml(input_file) or raw_mime

    mime = mime_def.get(raw_mime)
    if mime is None:
        raise UnsupportedMimeTypeError(raw_mime)
    return input_file.rename(input_file.with_suffix(mime.lower())), mime, raw_mime
