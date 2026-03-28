"""Tests for spreadsheet._excel2txt and internal helpers."""

import re
from pathlib import Path

import pytest

from unittest.mock import MagicMock

from spreadsheet import _excel2txt, excel2txt, EmptySpreadsheetError, _strip_empty, _df_to_md
from settings import settings
import pandas as pd

FIXTURES = Path(__file__).resolve().parent / "fixtures"


# ---------------------------------------------------------------------------
#  Helper
# ---------------------------------------------------------------------------

def _sheet_headers(md: str) -> list[str]:
    """Extract ## sheet headers from markdown output."""
    return re.findall(r"^## (.+)$", md, re.MULTILINE)


def _table_blocks(md: str) -> list[str]:
    """Extract pipe-table blocks (consecutive lines containing '|')."""
    blocks: list[str] = []
    current: list[str] = []
    for line in md.splitlines():
        if "|" in line:
            current.append(line)
        elif current:
            blocks.append("\n".join(current))
            current = []
    if current:
        blocks.append("\n".join(current))
    return blocks


# ===================================================================
#  1. All sheets empty → EmptySpreadsheetError
# ===================================================================

class TestEmptySheets:
    def test_all_empty_raises(self):
        with pytest.raises(EmptySpreadsheetError, match=r"3 sheet\(s\) contain no cell data"):
            _excel2txt(FIXTURES / "all_sheets_empty.xlsx")

    def test_whitespace_only_does_not_raise(self):
        """Cells with whitespace (' ') are not '' — they survive stripping."""
        md, _ = _excel2txt(FIXTURES / "whitespace_only_rows.xlsx")
        assert "Whitespace" in md


# ===================================================================
#  2. Mixed empty + data → should succeed, only non-empty sheets
# ===================================================================

class TestMixedEmpty:
    def test_mixed_returns_data_sheet(self):
        md, _ = _excel2txt(FIXTURES / "mixed_empty_and_data.xlsx")
        headers = _sheet_headers(md)
        assert "HasData" in headers
        # "Empty" sheet should NOT appear (no content after stripping)
        assert "Empty" not in headers

    def test_many_empty_one_tiny(self):
        md, _ = _excel2txt(FIXTURES / "many_empty_one_tiny.xlsx")
        headers = _sheet_headers(md)
        assert "Tiny" in headers
        # The 9 empty sheets should not produce headers
        assert len(headers) == 1


# ===================================================================
#  3. Duplicate empty-name columns (iloc bug fix)
# ===================================================================

class TestDuplicateEmptyColumns:
    def test_no_crash(self):
        """Must not raise ValueError about ambiguous Series truth value."""
        md, _ = _excel2txt(FIXTURES / "duplicate_empty_columns.xlsx")
        assert "ID" in md
        assert "Alice" in md

    def test_strip_empty_with_duplicate_cols(self):
        """Direct unit test: _strip_empty with multiple '' columns."""
        df = pd.DataFrame(
            {"A": ["1", "2"], "": ["", ""], "B": ["x", "y"]},
        )
        # Add another '' column — simulates multiple Unnamed columns
        df.insert(3, "", ["", ""], allow_duplicates=True)
        result = _strip_empty(df)
        # Both '' columns are fully empty → should be dropped
        assert "" not in list(result.columns)
        assert list(result.columns) == ["A", "B"]

    def test_strip_empty_keeps_nonempty_unnamed(self):
        """An unnamed column with data should be kept."""
        df = pd.DataFrame(
            {"A": ["1", "2"], "": ["val", ""]},
        )
        result = _strip_empty(df)
        assert "" in list(result.columns)


# ===================================================================
#  4. Error handling (mask vs short labels)
# ===================================================================

class TestErrorCells:
    def test_all_error_types_masked(self, monkeypatch):
        """With mask=True, error cells become empty strings."""
        monkeypatch.setattr(settings, "excel_mask_cell_errors", True)
        md, _ = _excel2txt(FIXTURES / "all_error_types.xlsx")
        # No raw error strings or short labels should appear
        for err in ("#REF!", "#N/A", "#VALUE!", "#NAME?", "#DIV/0!", "#NULL!", "#NUM!"):
            assert err not in md
        for label in ("#ref", "#n/a", "#val", "#name", "#div", "#null", "#num"):
            assert label not in md

    def test_all_error_types_short_labels(self, monkeypatch):
        """With mask=False, error cells get short labels."""
        monkeypatch.setattr(settings, "excel_mask_cell_errors", False)
        md, _ = _excel2txt(FIXTURES / "all_error_types.xlsx")
        # Raw error strings should not appear
        for err in ("#REF!", "#N/A", "#VALUE!", "#NAME?", "#DIV/0!", "#NULL!", "#NUM!"):
            assert err not in md
        # Short labels should be present
        for label in ("#ref", "#n/a", "#val", "#name", "#div", "#null", "#num"):
            assert label in md

    def test_error_headers_masked(self, monkeypatch):
        """With mask=True, error column headers become empty strings."""
        monkeypatch.setattr(settings, "excel_mask_cell_errors", True)
        md, _ = _excel2txt(FIXTURES / "all_error_types.xlsx")
        assert "#REF!" not in md

    def test_error_headers_short_labels(self, monkeypatch):
        """With mask=False, error column headers become short labels."""
        monkeypatch.setattr(settings, "excel_mask_cell_errors", False)
        md, _ = _excel2txt(FIXTURES / "all_error_types.xlsx")
        assert "#REF!" not in md
        # Header is now the short label
        assert "#ref" in md

    def test_error_only_in_headers_not_renamed(self):
        """Headers with error values are NOT renamed when data cells have no errors.

        _rename_error_columns only runs when sheet_has_errors is True (data cells).
        The error_headers fixture has clean data → headers stay as-is.
        """
        md, _ = _excel2txt(FIXTURES / "error_headers.xlsx")
        assert "Normal" in md
        # Headers keep their original error values (no data errors to trigger rename)
        assert "#REF!" in md

    def test_multi_sheet_mixed_errors(self):
        md, _ = _excel2txt(FIXTURES / "multi_sheet_mixed_errors.xlsx")
        headers = _sheet_headers(md)
        assert "Clean" in headers
        assert "Broken" in headers
        assert "AlsoClean" in headers
        assert "Widget" in md
        assert "9.99" in md


# ===================================================================
#  5. NaN flood
# ===================================================================

class TestNanFlood:
    def test_nan_flood_masked(self, monkeypatch):
        """500×20 cells of 'nan' → masked to '' → cleaned md empty, but pre_clean has content.
        No EmptySpreadsheetError should be raised (file has real structure, PDF fallback would
        reproduce the same NANs via OCR). The cleaned md is empty; main.py returns 422 instead."""
        monkeypatch.setattr(settings, "excel_mask_cell_errors", True)
        md, pre_clean_bytes = _excel2txt(FIXTURES / "nan_flood.xlsx")
        assert not md.strip(), "cleaned md should be empty after masking all NANs"
        assert pre_clean_bytes > 0, "pre_clean should reflect the NAN-filled content"

    def test_nan_flood_short_labels(self, monkeypatch):
        """With mask disabled, 'nan' cells become '#nan' short labels."""
        monkeypatch.setattr(settings, "excel_mask_cell_errors", False)
        md, _ = _excel2txt(FIXTURES / "nan_flood.xlsx", table_format="llm")
        assert "#nan" in md
        # Raw 'nan' string should not appear as-is
        assert "|nan|" not in md

    def test_float_nan_from_merged_cells(self):
        """Float NaN (from merged cells, missing formulas) must not leak as 'nan'."""
        md, _ = _excel2txt(FIXTURES / "float_nan_merged.xlsx")
        assert "nan" not in md.lower()
        assert "Data" in md


# ===================================================================
#  6. Content correctness
# ===================================================================

class TestContent:
    def test_single_cell(self):
        md, _ = _excel2txt(FIXTURES / "single_cell.xlsx")
        assert "lonely value" in md
        tables = _table_blocks(md)
        assert len(tables) == 1

    def test_wide_table(self):
        md, _ = _excel2txt(FIXTURES / "wide_100_columns.xlsx")
        tables = _table_blocks(md)
        assert len(tables) == 1
        # Check a few headers exist
        assert "H0" in md
        assert "H99" in md

    def test_multiline_normalized(self):
        """Newlines in cells should be normalized to spaces."""
        md, _ = _excel2txt(FIXTURES / "multiline_and_special_chars.xlsx")
        # Original cell had "Line1\nLine2\nLine3" → should be flattened
        assert "Line1 Line2 Line3" in md
        # CJK content preserved
        assert "日本語テスト" in md


# ===================================================================
#  7. _df_to_md empty DataFrame
# ===================================================================

class TestDfToMd:
    def test_empty_df_returns_empty_string(self):
        df = pd.DataFrame(columns=["A", "B"])
        assert _df_to_md(df, "llm") == ""
        assert _df_to_md(df, "human") == ""

    def test_non_empty_returns_pipe_table(self):
        df = pd.DataFrame({"X": ["1"], "Y": ["2"]})
        md = _df_to_md(df, "llm")
        assert "|" in md
        assert "X" in md

    def test_llm_format_compact(self):
        df = pd.DataFrame({"Col": ["value"]})
        llm = _df_to_md(df, "llm")
        human = _df_to_md(df, "human")
        # LLM format should be more compact (no padding spaces)
        assert len(llm) <= len(human)


# ===================================================================
#  8. Multiple tables (sheets) — independent schemas
# ===================================================================

class TestMultiTableIndependent:
    def test_all_sheets_present(self):
        md, _ = _excel2txt(FIXTURES / "multi_table_independent.xlsx")
        headers = _sheet_headers(md)
        assert headers == ["Employees", "Products", "Orders"]

    def test_all_tables_rendered(self):
        md, _ = _excel2txt(FIXTURES / "multi_table_independent.xlsx")
        tables = _table_blocks(md)
        assert len(tables) == 3

    def test_data_integrity_across_sheets(self):
        md, _ = _excel2txt(FIXTURES / "multi_table_independent.xlsx")
        # Employees data
        assert "Alice" in md
        assert "Engineering" in md
        # Products data
        assert "Widget" in md
        assert "A001" in md
        # Orders data
        assert "ORD-001" in md
        assert "2025-01-15" in md


# ===================================================================
#  9. Multiple sheets with varying sizes
# ===================================================================

class TestMultiTableVaryingSizes:
    def test_all_sheets_present(self):
        md, _ = _excel2txt(FIXTURES / "multi_table_varying_sizes.xlsx")
        headers = _sheet_headers(md)
        assert headers == ["Tiny", "Medium", "Small"]

    def test_medium_sheet_row_count(self):
        md, _ = _excel2txt(FIXTURES / "multi_table_varying_sizes.xlsx")
        # Medium sheet has 50 data rows; check first and last
        assert "item_0" in md
        assert "item_49" in md


# ===================================================================
# 10. Sheet names with special characters
# ===================================================================

class TestMultiTableSpecialNames:
    def test_special_names_preserved(self):
        md, _ = _excel2txt(FIXTURES / "multi_table_special_names.xlsx")
        headers = _sheet_headers(md)
        assert "Q1 2025 (Jan-Mar)" in headers
        assert "Données & Stats" in headers
        assert "Sheet #3 - Notes" in headers

    def test_data_in_special_named_sheets(self):
        md, _ = _excel2txt(FIXTURES / "multi_table_special_names.xlsx")
        assert "12000" in md
        assert "42.5" in md


# ===================================================================
# 11. Overlapping column names across sheets
# ===================================================================

class TestMultiTableOverlappingColumns:
    def test_both_years_present(self):
        md, _ = _excel2txt(FIXTURES / "multi_table_overlapping_columns.xlsx")
        headers = _sheet_headers(md)
        assert "2024" in headers
        assert "2025" in headers
        assert "Summary" in headers

    def test_distinct_data_per_sheet(self):
        """Same column names but different data per sheet."""
        md, _ = _excel2txt(FIXTURES / "multi_table_overlapping_columns.xlsx")
        # 2024 sheet: Bob fails
        assert "Fail" in md
        # Summary sheet: pass rate
        assert "100%" in md


# ===================================================================
# 12. Sparse multi-table — empty sheets interspersed
# ===================================================================

class TestMultiTableSparse:
    def test_only_data_sheets_appear(self):
        md, _ = _excel2txt(FIXTURES / "multi_table_sparse.xlsx")
        headers = _sheet_headers(md)
        assert "Data1" in headers
        assert "Data2" in headers
        assert "Data3" in headers
        # Empty sheets should not appear
        assert "EmptyA" not in headers
        assert "EmptyB" not in headers
        assert "EmptyC" not in headers

    def test_correct_table_count(self):
        md, _ = _excel2txt(FIXTURES / "multi_table_sparse.xlsx")
        tables = _table_blocks(md)
        assert len(tables) == 3

    def test_sheet_order_preserved(self):
        """Data sheets keep their original order despite empty sheets between them."""
        md, _ = _excel2txt(FIXTURES / "multi_table_sparse.xlsx")
        headers = _sheet_headers(md)
        assert headers == ["Data1", "Data2", "Data3"]


# ===================================================================
# 13. Public excel2txt wrapper
# ===================================================================

class TestExcel2TxtWrapper:
    def test_delegates_to_internal(self):
        """excel2txt returns the same result as _excel2txt for a normal file."""
        lo_server = MagicMock()
        md, _ = excel2txt(
            FIXTURES / "multi_table_independent.xlsx",
            table_format="llm",
            raw_mime=".xlsx",
            lo_server=lo_server,
        )
        headers = _sheet_headers(md)
        assert headers == ["Employees", "Products", "Orders"]
        # LibreOffice server should not be called for a normal .xlsx
        lo_server.convert_xls_to_xlsx.assert_not_called()
