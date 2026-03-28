"""Spreadsheet (Excel / ODS) → Markdown conversion.

Handles cell error detection, masking/labeling, empty row/column stripping,
and optional artifact saving for debugging.
"""

import logging
import re
from pathlib import Path

import pandas as pd
from tabulate import tabulate
from xlrd.biffh import XLRDError

from libreoffice import LibreOfficeServer

from settings import ExcelOutputFormat, settings
from debug import save_cell_error_artifacts

logger = logging.getLogger(__name__)


class EmptySpreadsheetError(Exception):
    """Raised when all sheets in a spreadsheet are empty after stripping."""

    pass

# ---------------------------------------------------------------------------
#  Excel cell error definitions
# ---------------------------------------------------------------------------

# Error patterns → short labels (case-insensitive exact match)
_EXCEL_ERRORS_SHORT: dict[str, str] = {
    "#REF!": "#ref",
    "#N/A": "#n/a",
    "#VALUE!": "#val",
    "#NAME?": "#name",
    "#DIV/0!": "#div",
    "#NULL!": "#null",
    "#NUM!": "#num",
    "nan": "#nan",
}

# Regex matching any error value (anchored, case-insensitive)
_CELL_ERROR_RE = re.compile(
    "|".join(re.escape(e) for e in _EXCEL_ERRORS_SHORT),
    re.IGNORECASE,
)

# Pre-built replacement dicts (keys are uppercased for case-insensitive lookup)
_ERROR_TO_SHORT: dict[str, str] = {k.upper(): v for k, v in _EXCEL_ERRORS_SHORT.items()}
_ERROR_TO_SHORT["NAN"] = "#nan"
_ERROR_TO_EMPTY: dict[str, str] = {k: "" for k in _ERROR_TO_SHORT}


# ---------------------------------------------------------------------------
#  Internal helpers
# ---------------------------------------------------------------------------

_SEPARATOR_RE = re.compile(r"\|[-:\s]+(?:\|[-:\s]+)*\|")


def _compact_table(table: str) -> str:
    """Reduce a tabulate pipe table to minimal tokens for LLM consumption.

    - Strip padding spaces inside cells: ``| value  |`` → ``|value|``
    - Shorten separator lines: ``|:------|:------|`` → ``|---|---|``
    """
    lines: list[str] = []
    for line in table.splitlines():
        if _SEPARATOR_RE.fullmatch(line.strip()):
            n_cols = line.count("|") - 1
            lines.append("|" + "|".join(["---"] * n_cols) + "|")
        else:
            parts = line.split("|")
            parts = [p.strip() for p in parts]
            lines.append("|".join(parts))
    return "\n".join(lines)


def _is_error_cell(val: object) -> bool:
    """True if a cell value is an Excel error or stringified NaN."""
    return isinstance(val, str) and bool(_CELL_ERROR_RE.fullmatch(val.strip()))


def _apply_error_replacement(
    df: pd.DataFrame, replacement_map: dict[str, str]
) -> pd.DataFrame:
    """Replace error cell values using a pre-built map (case-insensitive)."""

    def _replace(val: object) -> object:
        if not isinstance(val, str):
            return val
        return replacement_map.get(val.strip().upper(), val)

    return df.map(_replace)


def _rename_error_columns(
    df: pd.DataFrame, replacement_map: dict[str, str]
) -> pd.DataFrame:
    """Rename column headers that are Excel error values."""
    new_cols = {
        col: replacement_map.get(col.strip().upper(), col)
        for col in df.columns
        if isinstance(col, str) and col.strip().upper() in replacement_map
    }
    return df.rename(columns=new_cols) if new_cols else df


def _strip_empty(df: pd.DataFrame) -> pd.DataFrame:
    """Drop fully empty columns (header="" and all values="") and fully empty rows.

    Uses positional indexing (iloc) to avoid ambiguity when multiple columns
    share the same empty-string header name.
    """
    keep = [
        i
        for i, col in enumerate(df.columns)
        if col != "" or (df.iloc[:, i] != "").any()
    ]
    df = df.iloc[:, keep]
    return df[~(df == "").all(axis=1)]


def _df_to_md(df: pd.DataFrame, table_format: str) -> str:
    """Render a DataFrame as a Markdown pipe table.

    Returns an empty string when the DataFrame has no data rows.
    """
    if df.empty:
        return ""
    table = tabulate(df, headers="keys", tablefmt="pipe", showindex=False)
    if table_format == "llm":
        table = _compact_table(table)
    return table


# ---------------------------------------------------------------------------
#  Public API
# ---------------------------------------------------------------------------


def _excel2txt(path: Path, table_format: ExcelOutputFormat = "llm", raw_mime: str = "unknown") -> str:
    """Convert all sheets of an Excel / ODS file to Markdown tables.

    Reads settings from the global settings singleton:
    - ``excel_mask_cell_errors``: mask or shorten error cells.
    - ``save_cell_error_artifacts`` / ``cell_error_artifacts_dir``: artifact saving.

    After cell error handling, fully empty columns and rows are stripped.

    Args:
        path: Path to the spreadsheet file.
        table_format: ``"human"`` for aligned columns (standard tabulate pipe),
                      ``"llm"`` for minimal formatting (reduced tokens).
        raw_mime: Detected MIME type, used in artifact directory names.
    """
    engine = "odf" if path.suffix.lower() == ".ods" else None
    mask_errors = settings.excel_mask_cell_errors
    save_artifacts = settings.save_cell_error_artifacts
    error_map = _ERROR_TO_EMPTY if mask_errors else _ERROR_TO_SHORT

    try:
        sheets = pd.read_excel(
            path, sheet_name=None, dtype=str, keep_default_na=False, engine=engine
        )
        has_any_errors = False
        txt = ""
        txt_with_errors = ""

        for sheet_name, df in sheets.items():
            # Convert any remaining float NaN (merged cells, formula errors, etc.)
            # to empty strings — keep_default_na=False doesn't catch all cases.
            df = df.fillna("")
            df.columns = [c if isinstance(c, str) else "" for c in df.columns]

            # Rename columns: clear "Unnamed: X" headers, normalize whitespace
            col_dic = {}
            for col in df.columns:
                if isinstance(col, str):
                    if col.startswith("Unnamed: "):
                        col_dic[col] = ""
                    else:
                        col_dic[col] = (
                            col.replace("\n", " ").replace("\r", " ").replace("  ", " ")
                        )
            df = df.rename(columns=col_dic)

            # Normalize cell whitespace
            df = df.map(
                lambda x: (
                    x.replace("\n", " ").replace("\r", " ").replace("  ", " ")
                    if isinstance(x, str)
                    else x
                )
            )

            # Detect errors (vectorized: map + any)
            sheet_has_errors = df.map(_is_error_cell).any().any()
            has_any_errors = has_any_errors or sheet_has_errors

            # Artifact: render md with short error labels before masking
            if save_artifacts:
                df_labeled = (
                    _rename_error_columns(
                        _apply_error_replacement(df, _ERROR_TO_SHORT), _ERROR_TO_SHORT
                    )
                    if sheet_has_errors
                    else df
                )
                txt_with_errors += f"\n## {sheet_name}\n\n"
                txt_with_errors += (
                    _df_to_md(_strip_empty(df_labeled), table_format) + "\n\n"
                )

            # Apply error handling (mask → "" or shorten → #ref, #n/a, ...)
            if sheet_has_errors:
                df = _rename_error_columns(df, error_map)
                df = _apply_error_replacement(df, error_map)

            df = _strip_empty(df)
            md = _df_to_md(df, table_format)
            if md:
                txt += f"\n## {sheet_name}\n\n{md}\n\n"

        if not txt.strip():
            raise EmptySpreadsheetError(
                f"All {len(sheets)} sheet(s) are empty after stripping"
            )

        if save_artifacts and has_any_errors:
            save_cell_error_artifacts(
                input_path=path,
                md_with_errors=txt_with_errors,
                md_final=txt if mask_errors else None,
                artifacts_dir=Path(settings.artifact_dir) / settings.cell_error_artifacts_subdir,
                raw_mime=raw_mime,
            )

    except EmptySpreadsheetError:
        raise
    except XLRDError:
        raise
    except Exception as e:
        logger.error(
            f"Error during spreadsheet {path.suffix.lower()} -> MarkDown conversion: {e}"
        )
        raise e
    return txt


def is_encrypted_xls_error(exc: Exception) -> bool:
    """True if the exception is an xlrd 'Workbook is encrypted' error."""
    return isinstance(exc, XLRDError) and "encrypted" in str(exc).lower()


def excel2txt(
    path: Path,
    table_format: ExcelOutputFormat,
    raw_mime: str,
    lo_server: LibreOfficeServer,
) -> str:
    """Try _excel2txt
    On legacy-encrypted .xls, try convert via LibreOffice and retry (it could be empty password encrypted).

    Args:
        path: Path to the spreadsheet file.
        table_format: "human" or "llm".
        raw_mime: Detected MIME extension (.xls, .xlsx, .ods).
        lo_server: LibreOffice server instance for XLS → XLSX conversion.
    """
    try:
        return _excel2txt(path, table_format=table_format, raw_mime=raw_mime)
    except XLRDError as e:
        if raw_mime != ".xls" or not is_encrypted_xls_error(e):
            raise
        logger.warning("Legacy-encrypted XLS — converting to XLSX via LibreOffice")
        xlsx_path = path.with_suffix(".xlsx")
        lo_server.convert_xls_to_xlsx(path, xlsx_path)
        return _excel2txt(xlsx_path, table_format=table_format, raw_mime=raw_mime)
