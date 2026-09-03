"""Spreadsheet (Excel / ODS) → Markdown conversion.

Handles cell error detection, masking/labeling, empty row/column stripping,
optional artifact saving for debugging, and assembly of the final Markdown
document (header + table of contents with exact line numbers).
"""

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from tabulate import tabulate
from xlrd.biffh import XLRDError

from libreoffice import LibreOfficeServer

from schemas import SpreadsheetMode
from settings import TableOutputFormat, settings
from table_utils import _compact_table, _render_md_table
from debug import save_cell_error_artifacts

logger = logging.getLogger(__name__)


class EmptySpreadsheetError(Exception):
    """Raised when all sheets in a spreadsheet are empty after stripping."""

    pass


# ---------------------------------------------------------------------------
#  Value objects
# ---------------------------------------------------------------------------
# These are internal: built and consumed by our own code, never parsed from
# untrusted input and never serialized — hence plain frozen dataclasses rather
# than pydantic models (which, in this codebase, mark boundary types).


@dataclass(frozen=True, slots=True)
class SheetAnchor:
    """A ``## <name>`` heading and its 1-based line number inside a Markdown body."""

    name: str
    line: int


@dataclass(frozen=True, slots=True)
class SpreadsheetConversion:
    """Result of the pandas cell-extraction conversion.

    ``pre_clean_bytes`` is the UTF-8 size of the Markdown *before* error masking
    and empty stripping — used for the sparse-fallback ratio check so that files
    heavy with NaN/errors are not incorrectly treated as sparse.
    """

    markdown: str
    pre_clean_bytes: int
    anchors: tuple[SheetAnchor, ...]


@dataclass(frozen=True, slots=True)
class SpreadsheetDocument:
    """Final Markdown document, with the fate of each conversion section."""

    markdown: str
    kept: tuple[str, ...]  # section labels that carry real content
    dropped: tuple[str, ...]  # section labels removed for exceeding the size budget


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


def _excel2txt(
    path: Path, table_format: TableOutputFormat = "llm", raw_mime: str = "unknown"
) -> SpreadsheetConversion:
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

    Returns:
        A SpreadsheetConversion carrying the cleaned Markdown, its pre-clean size
        and one anchor per rendered sheet.
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
        txt_pre_clean = ""
        anchors: list[SheetAnchor] = []

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

            # Capture pre-clean Markdown (before error replacement and empty stripping)
            # Used for sparse-fallback ratio check: a file full of NANs should not be
            # mistaken for a sparse file just because the cleaned output is small.
            pre_clean_md = _df_to_md(df, table_format)
            if pre_clean_md:
                txt_pre_clean += f"\n## {sheet_name}\n\n{pre_clean_md}\n\n"

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
                # The heading lands on the line right after the ones already emitted:
                # the leading "\n" closes the current line, "## …" opens the next one.
                anchors.append(SheetAnchor(sheet_name, txt.count("\n") + 2))
                txt += f"\n## {sheet_name}\n\n{md}\n\n"

        # Raise only when the file is truly empty (no cell data at all, before any
        # cleaning). If pre_clean has content but txt is empty (e.g. all cells were
        # error values and mask_errors=True zeroed them out), do NOT fall back to PDF —
        # the PDF would show the same errors and OCR would reproduce them.
        if not txt_pre_clean.strip():
            raise EmptySpreadsheetError(
                f"All {len(sheets)} sheet(s) contain no cell data"
            )

        if save_artifacts and has_any_errors:
            save_cell_error_artifacts(
                input_path=path,
                md_with_errors=txt_with_errors,
                md_final=txt if mask_errors else None,
                artifacts_dir=Path(settings.artifact_dir)
                / settings.cell_error_artifacts_subdir,
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
    return SpreadsheetConversion(
        markdown=txt,
        pre_clean_bytes=len(txt_pre_clean.encode("utf-8")),
        anchors=tuple(anchors),
    )


def is_encrypted_xls_error(exc: Exception) -> bool:
    """True if the exception is an xlrd 'Workbook is encrypted' error."""
    return isinstance(exc, XLRDError) and "encrypted" in str(exc).lower()


def excel2txt(
    path: Path,
    table_format: TableOutputFormat,
    raw_mime: str,
    lo_server: LibreOfficeServer,
) -> SpreadsheetConversion:
    """Try _excel2txt
    On legacy-encrypted .xls, try convert via LibreOffice and retry (it could be empty password encrypted).

    Args:
        path: Path to the spreadsheet file.
        table_format: "human" or "llm".
        raw_mime: Detected MIME extension (.xls, .xlsx, .ods).
        lo_server: LibreOffice server instance for XLS → XLSX conversion.

    Returns:
        A SpreadsheetConversion. See _excel2txt for details.
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


# ---------------------------------------------------------------------------
#  OCR section assembly
# ---------------------------------------------------------------------------

PANDAS_SECTION = "pandas"
OCR_SECTION = "ocr"

_SECTION_TITLES: dict[str, str] = {
    PANDAS_SECTION: "# Cell extraction (pandas)",
    OCR_SECTION: "# Visual rendering (PaddleOCR)",
}

_METHOD_SECTIONS: dict[SpreadsheetMode, tuple[str, ...]] = {
    SpreadsheetMode.PANDAS: (PANDAS_SECTION,),
    SpreadsheetMode.OCR: (OCR_SECTION,),
    SpreadsheetMode.BOTH: (PANDAS_SECTION, OCR_SECTION),
}

_METHOD_BLURBS: dict[SpreadsheetMode, str] = {
    SpreadsheetMode.PANDAS: "cell values read with pandas",
    SpreadsheetMode.OCR: "PaddleOCR over a PDF rendering of the sheets",
    SpreadsheetMode.BOTH: (
        "cell values read with pandas, then PaddleOCR over a PDF rendering of the sheets"
    ),
}


def resolve_strategy(mode: SpreadsheetMode) -> tuple[bool, bool]:
    """(run_pandas, run_ocr) for a requested mode, before any fallback.

    ``auto`` starts as pandas-only and may switch the OCR flag on later, once the
    conversion has shown the file to be empty or sparse.
    """
    return (
        mode is not SpreadsheetMode.OCR,
        mode in (SpreadsheetMode.OCR, SpreadsheetMode.BOTH),
    )


def is_sparse(
    conversion: SpreadsheetConversion,
    file_size: int,
    min_input_mb: float,
    min_output_ratio: float,
) -> bool:
    """True when a spreadsheet yields so little Markdown that OCR is worth trying.

    Compares against the *pre-clean* Markdown size (before error masking and
    empty-row stripping) so that a NaN/error-heavy file is not mistaken for a
    sparse one: it has real cell data, and OCR would only reproduce the errors.

    Files below ``min_input_mb`` are never considered: rendering a small file to
    PDF costs more than the little it could add.
    """
    if file_size < min_input_mb * 1024 * 1024:
        return False
    return conversion.pre_clean_bytes < file_size * min_output_ratio


def build_ocr_body(
    pages: Sequence[str],
    sheet_page_counts: Sequence[tuple[str, int]] | None,
) -> tuple[str, tuple[SheetAnchor, ...]]:
    """Assemble per-page OCR Markdown into one section with ``## `` anchors.

    When ``sheet_page_counts`` (from LibreOffice) accounts for exactly the number of
    pages Paddle returned, pages are grouped per sheet and each group gets a
    ``## <sheet name>`` heading. Otherwise the mapping cannot be trusted — a heading
    per page (``## Page N``) is emitted instead. Line numbers are exact either way.

    Args:
        pages: Post-processed Markdown, one entry per PDF page, in order.
        sheet_page_counts: (sheet name, page count) pairs, or None when unavailable.

    Returns:
        (markdown, anchors) — anchor line numbers are 1-based within ``markdown``.
    """
    expected = (
        sum(count for _, count in sheet_page_counts) if sheet_page_counts else None
    )
    if sheet_page_counts and expected == len(pages):
        groups: list[tuple[str, Sequence[str]]] = []
        start = 0
        for name, count in sheet_page_counts:
            if count > 0:
                groups.append((name, pages[start : start + count]))
                start += count
    else:
        if sheet_page_counts:
            logger.warning(
                f"Sheet→page map covers {expected} page(s) but the pipeline returned "
                f"{len(pages)} — falling back to a per-page table of contents"
            )
        groups = [(f"Page {i}", [page]) for i, page in enumerate(pages, start=1)]

    txt = ""
    anchors: list[SheetAnchor] = []
    for name, group in groups:
        # Pages within a sheet keep the pipeline's original "  \n" join.
        body = "  \n".join(group)
        if not body.strip():
            continue
        anchors.append(SheetAnchor(name, txt.count("\n") + 2))
        txt += f"\n## {name}\n\n{body}\n\n"
    return txt, tuple(anchors)


# ---------------------------------------------------------------------------
#  Final document assembly (header + table of contents)
# ---------------------------------------------------------------------------


def _escape_cell(text: str) -> str:
    """Make a sheet name safe inside a Markdown pipe table."""
    return text.replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def human_size(n_bytes: float) -> str:
    """Byte count in the largest unit that keeps it readable."""
    for unit, factor in (("MB", 1e6), ("kB", 1e3)):
        if n_bytes >= factor:
            return f"{n_bytes / factor:.1f} {unit}"
    return f"{n_bytes:.0f} B"


def _header_lines(
    mime_ext: str,
    method: SpreadsheetMode,
    entries: Sequence[tuple[str, int]],
    dropped: Sequence[str],
    table_format: TableOutputFormat,
) -> list[str]:
    """Render the document header. Its line count depends only on the number of
    entries and on whether sections were dropped — never on the line values, which
    is what lets the table of contents be numbered in a single pass."""
    lines = [
        "# Spreadsheet converted to Markdown",
        "",
        f"Source file type: `{mime_ext}` — conversion method: **{method.value}** "
        f"({_METHOD_BLURBS[method]}).",
    ]
    if dropped:
        lines.append(
            f"Omitted section(s): {', '.join(dropped)} — output too large, "
            f"see the note in the body."
        )
    lines += [
        "",
        "## Table of contents",
        "",
        "Line numbers refer to this document, first line included.",
        "",
    ]
    lines += _render_md_table(
        headers=["Section", "Line"],
        rows=[[_escape_cell(label), str(line)] for label, line in entries],
        table_format=table_format,
    ).splitlines()
    lines += ["", "---", ""]
    return lines


def build_spreadsheet_document(
    mime_ext: str,
    method: SpreadsheetMode,
    table_format: TableOutputFormat,
    pandas_md: str | None = None,
    pandas_anchors: Sequence[SheetAnchor] = (),
    ocr_md: str | None = None,
    ocr_anchors: Sequence[SheetAnchor] = (),
    input_bytes: int | None = None,
    max_output_ratio: float | None = None,
) -> SpreadsheetDocument:
    """Assemble the final Markdown document for a spreadsheet input.

    Prepends a header stating the conversion method and a table of contents giving
    the exact line number of every sheet, then the section bodies. A section whose
    Markdown exceeds ``input_bytes * max_output_ratio`` is replaced by a short note
    saying so and reported in ``SpreadsheetDocument.dropped``; the surviving
    section(s) stay intact and correctly numbered.

    Args:
        mime_ext: Detected extension of the source file (".xlsx", ".ods", …).
        method: Effective method — ``AUTO`` must be resolved by the caller.
        table_format: Rendering format of the table of contents.
        pandas_md / ocr_md: Section bodies, or None when the section produced nothing.
        pandas_anchors / ocr_anchors: Sheet anchors, 1-based within their own section.
        input_bytes: Source file size, used with ``max_output_ratio`` as size budget.
        max_output_ratio: Per-section budget as a ratio of ``input_bytes``.

    Raises:
        ValueError: if ``method`` is ``AUTO`` (the caller must resolve it first).
    """
    if method not in _METHOD_SECTIONS:
        raise ValueError(f"build_spreadsheet_document: unresolved method {method!r}")

    budget = (
        int(input_bytes * max_output_ratio)
        if input_bytes and max_output_ratio
        else None
    )
    bodies = {PANDAS_SECTION: pandas_md, OCR_SECTION: ocr_md}
    section_anchors = {PANDAS_SECTION: pandas_anchors, OCR_SECTION: ocr_anchors}
    labels = _METHOD_SECTIONS[method]
    multi_section = len(labels) > 1

    body_lines: list[str] = []
    entries: list[tuple[str, int]] = []
    kept: list[str] = []
    dropped: list[str] = []

    for label in labels:
        md = bodies[label]
        anchors: Sequence[SheetAnchor] = ()

        if md is None or not md.strip():
            content = ["> Section omitted: this conversion produced no content."]
        elif budget is not None and len(md.encode("utf-8")) > budget:
            assert input_bytes is not None and max_output_ratio is not None
            md_bytes = len(md.encode("utf-8"))
            dropped.append(label)
            content = [
                f"> Section omitted: this conversion produced {human_size(md_bytes)} of Markdown "
                f"from a {human_size(input_bytes)} input (ratio {md_bytes / input_bytes:.1f}x, "
                f"max {max_output_ratio}x)."
            ]
        else:
            kept.append(label)
            content = md.splitlines()
            anchors = section_anchors[label]

        if multi_section:
            body_lines.append(_SECTION_TITLES[label])
            # Section bodies usually already open with a blank line — don't add a second.
            if content and content[0].strip():
                body_lines.append("")

        # Anchor line numbers are 1-based within their own section body.
        offset = len(body_lines)
        body_lines += content
        body_lines.append("")
        for anchor in anchors:
            name = f"{label} — {anchor.name}" if multi_section else anchor.name
            entries.append((name, offset + anchor.line))

    # Two renders: the header's line count is independent of the numbers it carries,
    # so the first pass gives the exact offset to apply in the second.
    offset = len(_header_lines(mime_ext, method, entries, dropped, table_format))
    header = _header_lines(
        mime_ext,
        method,
        [(label, offset + line) for label, line in entries],
        dropped,
        table_format,
    )
    return SpreadsheetDocument(
        markdown="\n".join(header + body_lines),
        kept=tuple(kept),
        dropped=tuple(dropped),
    )
