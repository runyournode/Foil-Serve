"""End-to-end tests against a RUNNING foil-serve instance.

Skipped unless a server answers on ``$FOIL_SERVE_URL`` (default
``http://127.0.0.1:8081``), because they need the whole stack — GPU, the vLLM
serving PaddleOCR-VL, and LibreOffice:

    cd src/foil_serve && uv run uvicorn main:app --port 8081
    uv run pytest tests/live/ -v

They cover what the offline suite structurally cannot: that the sheet → page map
LibreOffice reports actually lines up with the pages PaddleOCR returns, so the
table of contents points at the right lines in the real output. The decisive
fixture is ``multipage_sheets.xlsx``, whose first sheet spans ~6 PDF pages —
the case that breaks under a naive "one sheet = one page" assumption.

Set ``FOIL_SERVE_API_KEY`` if the server does not use the key from
``server_config.toml``.
"""

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from settings import settings

BASE_URL = os.environ.get("FOIL_SERVE_URL", "http://127.0.0.1:8081").rstrip("/")
API_KEY = os.environ.get("FOIL_SERVE_API_KEY", (settings.app_api_keys or [""])[0])
FIXTURES = Path(__file__).resolve().parent.parent / "spreadsheet" / "fixtures"
DATA = Path(__file__).resolve().parent.parent / "data"

# Generous: an OCR conversion of a multi-page spreadsheet is real GPU work.
TIMEOUT_S = 600


def _server_is_up() -> bool:
    try:
        with urllib.request.urlopen(f"{BASE_URL}/health", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _server_is_up(), reason=f"no foil-serve instance answering on {BASE_URL}"
)


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------


def _decode(raw: bytes) -> dict | bytes:
    """Parse a JSON body, or hand back the raw bytes (the download routes
    return a zstd archive)."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return raw


def _post(route: str, file_path: Path) -> tuple[int, dict | bytes]:
    """POST a file to the server. Returns (status, parsed body or raw bytes)."""
    req = urllib.request.Request(
        f"{BASE_URL}{route}",
        data=file_path.read_bytes(),
        method="POST",
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/octet-stream",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return r.status, _decode(r.read())
    except urllib.error.HTTPError as e:
        return e.code, _decode(e.read())


def _process(route: str, fixture: str) -> dict:
    """POST a fixture and return the ProcessedDocument, failing on any error."""
    status, body = _post(route, FIXTURES / fixture)
    assert status == 200, f"{route} → {status}: {body}"
    return body


_TOC_ROW_RE = re.compile(r"^\|\s*(.+?)\s*\|\s*(\d+)\s*\|$")


def _toc(md: str) -> list[tuple[str, int]]:
    """(label, line) rows of the table of contents — header region only."""
    lines = md.splitlines()
    start = lines.index("## Table of contents")
    end = lines.index("---", start)
    rows = []
    for line in lines[start:end]:
        m = _TOC_ROW_RE.match(line.strip())
        if m and m.group(1) != "Section":
            rows.append((m.group(1), int(m.group(2))))
    return rows


def _method(md: str) -> str:
    """The conversion method the header advertises."""
    line = next(
        line for line in md.splitlines() if line.startswith("Source file type:")
    )
    return re.search(r"\*\*(\w+)\*\*", line).group(1)


def _assert_toc_is_exact(md: str, expected: list[str]) -> None:
    """Every announced line number must land on the heading it announces."""
    lines = md.splitlines()
    rows = _toc(md)
    assert [label for label, _ in rows] == expected
    for label, line in rows:
        assert 1 <= line <= len(lines), f"{label}: line {line} out of range"
        sheet = label.split(" — ")[-1]
        assert lines[line - 1] == f"## {sheet}", (
            f"{label}: line {line} is {lines[line - 1]!r}"
        )


# ---------------------------------------------------------------------------
#  1. Table of contents against the real pipeline
# ---------------------------------------------------------------------------

SHEETS = ["Budget", "Summary", "Notes"]


class TestTableOfContents:
    """`multipage_sheets.xlsx` has a first sheet spanning several PDF pages."""

    def test_pandas(self):
        doc = _process("/v1/process/spreadsheet_pandas", "multipage_sheets.xlsx")
        md = doc["page_content"]
        assert _method(md) == "pandas"
        assert doc["images"] == {}
        _assert_toc_is_exact(md, SHEETS)

    def test_ocr_groups_multi_page_sheets(self):
        """The 6 pages of `Budget` must sit under one anchor, with `Summary`
        after them — the assertion that fails if the page map is ignored."""
        doc = _process("/v1/process/spreadsheet_ocr", "multipage_sheets.xlsx")
        md = doc["page_content"]
        assert _method(md) == "ocr"
        _assert_toc_is_exact(md, SHEETS)

        rows = dict(_toc(md))
        assert rows["Summary"] - rows["Budget"] > 100, (
            "Budget spans several PDF pages; Summary must start well after it"
        )

    def test_both_offsets_the_ocr_section(self):
        doc = _process("/v1/process/spreadsheet_both", "multipage_sheets.xlsx")
        md = doc["page_content"]
        assert _method(md) == "both"
        _assert_toc_is_exact(
            md, [f"pandas — {s}" for s in SHEETS] + [f"ocr — {s}" for s in SHEETS]
        )

    def test_ods(self):
        """The odfpy engine and its LibreOffice rendering."""
        for mode in ("pandas", "ocr"):
            doc = _process(f"/v1/process/spreadsheet_{mode}", "multi_sheet.ods")
            md = doc["page_content"]
            assert _method(md) == mode
            assert "`.ods`" in md
            _assert_toc_is_exact(md, ["First", "Second"])


# ---------------------------------------------------------------------------
#  2. Query-param equivalents of the fixed-mode routes
# ---------------------------------------------------------------------------


class TestQueryParam:
    @pytest.mark.parametrize(
        "mode,expected", [("auto", "pandas"), ("pandas", "pandas"), ("ocr", "ocr")]
    )
    def test_spreadsheet_mode(self, mode, expected):
        doc = _process(f"/v1/process?spreadsheet_mode={mode}", "multipage_sheets.xlsx")
        assert _method(doc["page_content"]) == expected

    def test_download_route_returns_the_document_in_an_archive(self):
        status, body = _post(
            "/v1/process/spreadsheet_pandas/download",
            FIXTURES / "multipage_sheets.xlsx",
        )
        assert status == 200
        assert isinstance(body, bytes) and body[:4] == b"\x28\xb5\x2f\xfd", (
            "expected a zstd archive"
        )

        import io
        import tarfile

        import zstandard

        with tarfile.open(
            fileobj=io.BytesIO(zstandard.ZstdDecompressor().decompress(body, 64 << 20))
        ) as tar:
            names = tar.getnames()
            md = tar.extractfile("page_content.md").read().decode()
        assert {"page_content.md", "metadata.json", "mime.txt"} <= set(names)
        assert _method(md) == "pandas"
        _assert_toc_is_exact(md, SHEETS)


# ---------------------------------------------------------------------------
#  3. The `auto` sparse fallback and its per-request override
# ---------------------------------------------------------------------------


class TestSparseFallback:
    """`sparse_image_only.xlsx` is a big file with almost no cells — exactly what
    the fallback exists for. It sits above excel_min_input_for_fallback_mb, so
    the sparse check actually runs."""

    def test_auto_falls_back_to_ocr(self):
        doc = _process("/v1/process", "sparse_image_only.xlsx")
        assert _method(doc["page_content"]) == "ocr"
        assert doc["images"], "the embedded chart should come back as an image"

    def test_request_override_disables_the_fallback(self):
        doc = _process("/v1/process?excel_min_output_ratio=0", "sparse_image_only.xlsx")
        assert _method(doc["page_content"]) == "pandas", (
            "a threshold of 0 makes nothing sparse, so pandas must win"
        )
        assert "|Quarter|Revenue|" in doc["page_content"].replace(" ", "")

    def test_dense_file_of_the_same_size_is_not_sparse(self):
        """Counterpart: same size class, real cell data → pandas, no OCR.
        (Never send this fixture through `ocr`: it renders to hundreds of pages.)"""
        doc = _process("/v1/process", "big_dense.xlsx")
        assert _method(doc["page_content"]) == "pandas"


# ---------------------------------------------------------------------------
#  4. Failure modes
# ---------------------------------------------------------------------------


class TestFailureModes:
    def test_pandas_never_falls_back_on_an_empty_file(self):
        status, body = _post(
            "/v1/process/spreadsheet_pandas", FIXTURES / "all_sheets_empty.xlsx"
        )
        assert status == 422
        assert "'pandas'" in body["detail"]

    def test_auto_reaches_ocr_on_an_empty_file(self):
        """Nothing to read on blank pages either, but the message shows the
        fallback did run."""
        status, body = _post("/v1/process", FIXTURES / "all_sheets_empty.xlsx")
        assert status == 422
        assert "'ocr'" in body["detail"]


# ---------------------------------------------------------------------------
#  5. Non-spreadsheet inputs are unaffected by the mode
# ---------------------------------------------------------------------------


class TestOtherFileTypes:
    @pytest.mark.skipif(
        not (
            Path(__file__).resolve().parent.parent.parent / "src/foil_serve/init"
        ).exists(),
        reason="sample image not available",
    )
    def test_image_through_a_spreadsheet_route(self):
        img = (
            Path(__file__).resolve().parent.parent.parent
            / "src/foil_serve/init/test_pipeline.jpeg"
        )
        status, body = _post("/v1/process/spreadsheet_both", img)
        assert status == 200
        md = body["page_content"]
        assert not md.startswith("# Spreadsheet converted to Markdown"), (
            "the spreadsheet header must not be added to a non-spreadsheet input"
        )
        assert "magic" in md.lower()
