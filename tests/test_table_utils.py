"""Tests for table_utils: HTML table → Markdown conversion and pruning."""

import pytest

from bs4 import BeautifulSoup

from table_utils import _span_value, prune_tables, try_html_table_to_md


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

SIMPLE_TABLE = (
    "<table><tr><th>A</th><th>B</th></tr>"
    "<tr><td>1</td><td>2</td></tr>"
    "<tr><td>3</td><td>4</td></tr></table>"
)


def _md_rows(md: str) -> list[str]:
    return [line for line in md.splitlines() if line.strip()]


# ===================================================================
#  1. Convertible tables
# ===================================================================


class TestConvertible:
    def test_simple_table(self):
        md = try_html_table_to_md(SIMPLE_TABLE, "llm")
        assert md is not None
        rows = _md_rows(md)
        assert rows[0] == "|A|B|"
        assert rows[1] == "|---|---|"
        assert rows[2] == "|1|2|"
        assert rows[3] == "|3|4|"

    def test_thead_header(self):
        html = (
            "<table><thead><tr><th>A</th><th>B</th></tr></thead>"
            "<tbody><tr><td>1</td><td>2</td></tr></tbody></table>"
        )
        md = try_html_table_to_md(html, "llm")
        assert md is not None
        assert _md_rows(md)[0] == "|A|B|"
        assert _md_rows(md)[2] == "|1|2|"

    def test_explicit_spans_of_one_are_accepted(self):
        html = '<table><tr><th colspan="1">A</th></tr><tr><td rowspan="1">1</td></tr></table>'
        assert try_html_table_to_md(html, "llm") is not None

    def test_pipe_characters_are_escaped(self):
        html = "<table><tr><th>A</th></tr><tr><td>x|y</td></tr></table>"
        md = try_html_table_to_md(html, "llm")
        assert md is not None
        assert "x\\|y" in md

    def test_human_format_is_aligned(self):
        md = try_html_table_to_md(SIMPLE_TABLE, "human")
        assert md is not None
        # Aligned pipe tables pad cells with spaces (numeric columns are right-aligned)
        assert "|   A |" in md
        assert "|   1 |" in md


# ===================================================================
#  2. Empty <thead> (regression: used to raise AttributeError)
# ===================================================================


class TestEmptyThead:
    def test_empty_thead_falls_back_to_first_row(self):
        html = (
            "<table><thead></thead><tbody>"
            "<tr><td>A</td><td>B</td></tr>"
            "<tr><td>1</td><td>2</td></tr>"
            "</tbody></table>"
        )
        md = try_html_table_to_md(html, "llm")
        assert md is not None
        rows = _md_rows(md)
        assert rows[0] == "|A|B|"
        assert rows[2] == "|1|2|"


# ===================================================================
#  3. Non-convertible tables → None
# ===================================================================


class TestNonConvertible:
    def test_colspan_merged_cell(self):
        html = '<table><tr><th colspan="2">AB</th></tr><tr><td>1</td><td>2</td></tr></table>'
        assert try_html_table_to_md(html, "llm") is None

    def test_rowspan_merged_cell(self):
        html = (
            "<table><tr><th>A</th><th>B</th></tr>"
            '<tr><td rowspan="2">1</td><td>2</td></tr><tr><td>3</td></tr></table>'
        )
        assert try_html_table_to_md(html, "llm") is None

    def test_non_numeric_colspan(self):
        html = '<table><tr><th colspan="two">A</th></tr><tr><td>1</td></tr></table>'
        assert try_html_table_to_md(html, "llm") is None

    def test_nested_table(self):
        html = "<table><tr><td><table><tr><td>x</td></tr></table></td></tr></table>"
        assert try_html_table_to_md(html, "llm") is None

    def test_multi_row_thead(self):
        html = (
            "<table><thead><tr><th>A</th></tr><tr><th>B</th></tr></thead>"
            "<tbody><tr><td>1</td></tr></tbody></table>"
        )
        assert try_html_table_to_md(html, "llm") is None

    def test_inconsistent_column_count(self):
        html = "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td></tr></table>"
        assert try_html_table_to_md(html, "llm") is None

    def test_no_table_tag(self):
        assert try_html_table_to_md("<div>no table here</div>", "llm") is None

    def test_empty_table(self):
        assert try_html_table_to_md("<table></table>", "llm") is None


# ===================================================================
#  4. _span_value
# ===================================================================


class TestSpanValue:
    def _cell(self, html: str):
        return BeautifulSoup(html, "html.parser").find("td")

    def test_absent_attribute_defaults_to_one(self):
        assert _span_value(self._cell("<td>x</td>"), "colspan") == 1

    def test_string_value(self):
        assert _span_value(self._cell('<td colspan="3">x</td>'), "colspan") == 3

    def test_non_string_value_raises(self):
        cell = self._cell("<td>x</td>")
        cell.attrs["colspan"] = ["2", "3"]
        with pytest.raises(ValueError, match="non-string colspan"):
            _span_value(cell, "colspan")


# ===================================================================
#  5. prune_tables
# ===================================================================


class TestPruneTables:
    def test_convertible_table_replaced_with_md(self):
        md = prune_tables(f"before\n{SIMPLE_TABLE}\nafter")
        assert "<table" not in md
        assert "|A|B|" in md
        assert md.startswith("before")
        assert md.endswith("after")

    def test_non_convertible_table_kept_as_cleaned_html(self):
        html = (
            '<table class="fancy" style="color:red">'
            '<tr><th colspan="2">AB</th></tr><tr><td>1</td><td>2</td></tr></table>'
        )
        out = prune_tables(html)
        assert "<table" in out
        # Non-semantic attributes stripped, semantic ones kept
        assert "style" not in out
        assert "class" not in out
        assert 'colspan="2"' in out

    def test_text_without_tables_untouched(self):
        text = "# Title\n\nplain markdown, no tables."
        assert prune_tables(text) == text
