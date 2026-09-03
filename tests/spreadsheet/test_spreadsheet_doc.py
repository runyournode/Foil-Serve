"""Tests for the spreadsheet document assembly: OCR body grouping, header,
table of contents line numbers, and the per-section size budget.

The single invariant that matters for a RAG agent — and the one that breaks first
when the assembly changes — is that every line number in the table of contents
points at the matching ``## `` heading in the final document.
"""

import re
from pathlib import Path

import pytest

from spreadsheet import (
    SheetAnchor,
    _excel2txt,
    build_ocr_body,
    build_spreadsheet_document,
)
from schemas import SpreadsheetMode

FIXTURES = Path(__file__).resolve().parent / "fixtures"

_TOC_ROW_RE = re.compile(r"^\|\s*(.+?)\s*\|\s*(\d+)\s*\|$")


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------


def _toc(doc: str) -> list[tuple[str, int]]:
    """Parse the (label, line) rows of the table of contents.

    Only the header region (up to the first "---" rule) is scanned: body tables
    also have rows ending in a number and would otherwise be picked up.
    """
    lines = doc.splitlines()
    start = lines.index("## Table of contents")
    end = lines.index("---", start)
    rows: list[tuple[str, int]] = []
    for line in lines[start:end]:
        m = _TOC_ROW_RE.match(line.strip())
        if m and m.group(1) != "Section":
            rows.append((m.group(1), int(m.group(2))))
    return rows


def _assert_toc_points_at_headings(doc: str, expected_names: list[str]) -> None:
    """Every ToC line number must land on the ``## <name>`` heading it announces."""
    lines = doc.splitlines()
    rows = _toc(doc)
    assert [label for label, _ in rows] == expected_names
    for label, line in rows:
        assert 1 <= line <= len(lines), f"{label}: line {line} out of range"
        target = lines[line - 1]
        assert target.startswith("## "), f"{label}: line {line} is {target!r}"
        # The heading carries the sheet name, the ToC label may prefix the section.
        assert target[3:].strip() == label.split(" — ")[-1].replace("\\|", "|")


def _pandas_conversion(fixture: str = "multi_table_independent.xlsx"):
    return _excel2txt(FIXTURES / fixture, table_format="llm", raw_mime=".xlsx")


# ---------------------------------------------------------------------------
#  1. Anchors produced by the pandas conversion
# ---------------------------------------------------------------------------


class TestConversionAnchors:
    def test_anchors_point_at_sheet_headings(self):
        conv = _pandas_conversion()
        lines = conv.markdown.splitlines()
        assert [a.name for a in conv.anchors] == ["Employees", "Products", "Orders"]
        for anchor in conv.anchors:
            assert lines[anchor.line - 1] == f"## {anchor.name}"

    def test_empty_sheets_get_no_anchor(self):
        conv = _excel2txt(FIXTURES / "mixed_empty_and_data.xlsx")
        assert [a.name for a in conv.anchors] == ["HasData"]


# ---------------------------------------------------------------------------
#  2. OCR body assembly
# ---------------------------------------------------------------------------


class TestBuildOcrBody:
    def test_groups_pages_per_sheet(self):
        pages = ["p1", "p2", "p3", "p4"]
        md, anchors = build_ocr_body(pages, [("Budget", 3), ("Notes", 1)])
        assert [a.name for a in anchors] == ["Budget", "Notes"]
        lines = md.splitlines()
        for anchor in anchors:
            assert lines[anchor.line - 1] == f"## {anchor.name}"
        # Pages of a sheet keep the pipeline's "  \n" join.
        assert "p1  \np2  \np3" in md
        assert "p4" in md

    def test_falls_back_to_pages_when_map_disagrees(self):
        pages = ["p1", "p2", "p3"]
        md, anchors = build_ocr_body(pages, [("Budget", 5)])
        assert [a.name for a in anchors] == ["Page 1", "Page 2", "Page 3"]
        lines = md.splitlines()
        for anchor in anchors:
            assert lines[anchor.line - 1] == f"## {anchor.name}"

    def test_falls_back_to_pages_without_map(self):
        _, anchors = build_ocr_body(["only"], None)
        assert [a.name for a in anchors] == ["Page 1"]

    def test_blank_pages_are_skipped(self):
        md, anchors = build_ocr_body(["", "   ", "real"], None)
        assert [a.name for a in anchors] == ["Page 3"]
        assert "real" in md

    def test_zero_page_sheets_are_skipped(self):
        _, anchors = build_ocr_body(["p1"], [("Empty", 0), ("Data", 1)])
        assert [a.name for a in anchors] == ["Data"]

    def test_no_pages(self):
        md, anchors = build_ocr_body([], None)
        assert md == ""
        assert anchors == ()


# ---------------------------------------------------------------------------
#  3. Document assembly — table of contents exactness
# ---------------------------------------------------------------------------


class TestTableOfContents:
    def test_pandas_only(self):
        conv = _pandas_conversion()
        doc = build_spreadsheet_document(
            mime_ext=".xlsx",
            method=SpreadsheetMode.PANDAS,
            table_format="llm",
            pandas_md=conv.markdown,
            pandas_anchors=conv.anchors,
        )
        assert doc.kept == ("pandas",)
        assert doc.dropped == ()
        _assert_toc_points_at_headings(
            doc.markdown, ["Employees", "Products", "Orders"]
        )

    def test_ocr_only(self):
        ocr_md, ocr_anchors = build_ocr_body(["a", "b"], [("Sheet1", 2)])
        doc = build_spreadsheet_document(
            mime_ext=".ods",
            method=SpreadsheetMode.OCR,
            table_format="llm",
            ocr_md=ocr_md,
            ocr_anchors=ocr_anchors,
        )
        assert doc.kept == ("ocr",)
        _assert_toc_points_at_headings(doc.markdown, ["Sheet1"])

    def test_both_sections_offset_correctly(self):
        """The OCR anchors must be shifted by the whole pandas section."""
        conv = _pandas_conversion()
        ocr_md, ocr_anchors = build_ocr_body(
            ["page one", "page two", "page three"],
            [("Employees", 1), ("Products", 1), ("Orders", 1)],
        )
        doc = build_spreadsheet_document(
            mime_ext=".xlsx",
            method=SpreadsheetMode.BOTH,
            table_format="llm",
            pandas_md=conv.markdown,
            pandas_anchors=conv.anchors,
            ocr_md=ocr_md,
            ocr_anchors=ocr_anchors,
        )
        assert doc.kept == ("pandas", "ocr")
        _assert_toc_points_at_headings(
            doc.markdown,
            [
                "pandas — Employees",
                "pandas — Products",
                "pandas — Orders",
                "ocr — Employees",
                "ocr — Products",
                "ocr — Orders",
            ],
        )

    def test_human_table_format_stays_exact(self):
        conv = _pandas_conversion()
        doc = build_spreadsheet_document(
            mime_ext=".xlsx",
            method=SpreadsheetMode.PANDAS,
            table_format="human",
            pandas_md=conv.markdown,
            pandas_anchors=conv.anchors,
        )
        _assert_toc_points_at_headings(
            doc.markdown, ["Employees", "Products", "Orders"]
        )

    def test_single_sheet(self):
        doc = build_spreadsheet_document(
            mime_ext=".xlsx",
            method=SpreadsheetMode.PANDAS,
            table_format="llm",
            pandas_md="\n## Only\n\n|a|\n|---|\n|1|\n\n",
            pandas_anchors=(SheetAnchor("Only", 2),),
        )
        _assert_toc_points_at_headings(doc.markdown, ["Only"])

    def test_sheet_name_with_pipe_is_escaped(self):
        doc = build_spreadsheet_document(
            mime_ext=".xlsx",
            method=SpreadsheetMode.PANDAS,
            table_format="llm",
            pandas_md="\n## a|b\n\ncontent\n\n",
            pandas_anchors=(SheetAnchor("a|b", 2),),
        )
        assert "a\\|b" in doc.markdown
        _assert_toc_points_at_headings(doc.markdown, ["a\\|b"])

    def test_header_states_the_method_and_source(self):
        conv = _pandas_conversion()
        doc = build_spreadsheet_document(
            mime_ext=".xlsx",
            method=SpreadsheetMode.PANDAS,
            table_format="llm",
            pandas_md=conv.markdown,
            pandas_anchors=conv.anchors,
        )
        assert doc.markdown.startswith("# Spreadsheet converted to Markdown")
        assert "`.xlsx`" in doc.markdown
        assert "**pandas**" in doc.markdown

    def test_auto_is_rejected(self):
        with pytest.raises(ValueError, match="unresolved method"):
            build_spreadsheet_document(
                mime_ext=".xlsx",
                method=SpreadsheetMode.AUTO,
                table_format="llm",
                pandas_md="x",
            )


# ---------------------------------------------------------------------------
#  4. Per-section size budget
# ---------------------------------------------------------------------------


class TestSizeBudget:
    def _both(self, pandas_md: str, ocr_md: str, ratio: float | None):
        return build_spreadsheet_document(
            mime_ext=".xlsx",
            method=SpreadsheetMode.BOTH,
            table_format="llm",
            pandas_md=pandas_md,
            pandas_anchors=(SheetAnchor("P", 2),),
            ocr_md=ocr_md,
            ocr_anchors=(SheetAnchor("O", 2),),
            input_bytes=100,
            max_output_ratio=ratio,
        )

    def test_oversized_pandas_section_is_dropped(self):
        doc = self._both("\n## P\n\n" + "x" * 5000, "\n## O\n\nsmall\n", 1.0)
        assert doc.kept == ("ocr",)
        assert doc.dropped == ("pandas",)
        assert "Section omitted" in doc.markdown
        # The surviving section is still correctly indexed.
        _assert_toc_points_at_headings(doc.markdown, ["ocr — O"])

    def test_oversized_ocr_section_is_dropped(self):
        doc = self._both("\n## P\n\nsmall\n", "\n## O\n\n" + "x" * 5000, 1.0)
        assert doc.kept == ("pandas",)
        assert doc.dropped == ("ocr",)
        _assert_toc_points_at_headings(doc.markdown, ["pandas — P"])

    def test_both_oversized_leaves_nothing(self):
        doc = self._both("\n## P\n\n" + "x" * 5000, "\n## O\n\n" + "y" * 5000, 1.0)
        assert doc.kept == ()
        assert doc.dropped == ("pandas", "ocr")
        assert _toc(doc.markdown) == []

    def test_no_budget_keeps_everything(self):
        doc = self._both("\n## P\n\n" + "x" * 5000, "\n## O\n\n" + "y" * 5000, None)
        assert doc.kept == ("pandas", "ocr")
        assert doc.dropped == ()

    def test_empty_section_is_not_reported_as_dropped(self):
        doc = build_spreadsheet_document(
            mime_ext=".xlsx",
            method=SpreadsheetMode.BOTH,
            table_format="llm",
            pandas_md=None,
            ocr_md="\n## O\n\ncontent\n",
            ocr_anchors=(SheetAnchor("O", 2),),
            input_bytes=100,
            max_output_ratio=5.0,
        )
        assert doc.kept == ("ocr",)
        assert doc.dropped == ()
        assert "produced no content" in doc.markdown
        _assert_toc_points_at_headings(doc.markdown, ["ocr — O"])

    def test_nothing_at_all(self):
        doc = build_spreadsheet_document(
            mime_ext=".xlsx",
            method=SpreadsheetMode.PANDAS,
            table_format="llm",
            pandas_md="   ",
        )
        assert doc.kept == ()
        assert doc.dropped == ()
