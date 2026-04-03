"""Markdown table rendering and HTML-to-Markdown table conversion utilities.

Used by both the spreadsheet pipeline (excel2txt) and the OCR post-processing
pipeline (prune_tables).
"""

import re
import logging

from bs4 import BeautifulSoup
from tabulate import tabulate

logger = logging.getLogger(__name__)

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


def _render_md_table(headers: list[str], rows: list[list[str]], table_format: str) -> str:
    """Render a 2D table as a Markdown pipe table.

    Args:
        headers: Column header strings.
        rows: List of data rows (each row is a list of cell strings).
        table_format: ``"human"`` for aligned columns, ``"llm"`` for compact tokens.
    """
    table = tabulate(rows, headers=headers, tablefmt="pipe")
    if table_format == "llm":
        table = _compact_table(table)
    return table


def clean_html_table(html_table: str) -> str:
    """Strip non-semantic attributes and normalize whitespace in an HTML table."""
    soup = BeautifulSoup(html_table, "html.parser")

    # Whitelist of attributes essential for semantics
    semantic_attrs = {"colspan", "rowspan", "scope"}

    for tag in soup.find_all(True):
        # Keep only semantic attributes
        tag.attrs = {k: v for k, v in tag.attrs.items() if k.lower() in semantic_attrs}

        # Clean cells: remove non-breaking spaces and tidy up
        if tag.name in ["td", "th"]:
            clean_text = tag.get_text(strip=True)
            tag.string = clean_text

    cleaned_html = str(soup)
    cleaned_html = re.sub(r">\s+<", "><", cleaned_html)
    return cleaned_html.strip()


def try_html_table_to_md(html_table: str, table_format: str) -> str | None:
    """Try to convert an HTML table to a Markdown pipe table.

    Returns None if the table cannot be represented as MD without semantic loss.

    Non-convertible cases:
    - Any cell with colspan > 1 or rowspan > 1 (merged cells)
    - Nested tables
    - Inconsistent column count across rows
    - <thead> with more than one <tr> (hierarchical header — no MD equivalent)

    Pipe characters (``|``) in cell text are escaped as ``\\|``.
    """
    soup = BeautifulSoup(html_table, "html.parser")
    table = soup.find("table")
    if table is None:
        return None

    # Reject nested tables
    for cell in table.find_all(["td", "th"]):
        if cell.find("table"):
            return None

    # Reject merged cells
    for cell in table.find_all(["td", "th"]):
        try:
            if int(cell.get("colspan", 1)) > 1 or int(cell.get("rowspan", 1)) > 1:
                return None
        except (ValueError, TypeError):
            return None

    # Reject multi-row thead (hierarchical header — semantics cannot be preserved in MD)
    thead = table.find("thead")
    if thead and len(thead.find_all("tr")) > 1:
        return None

    all_trs = table.find_all("tr")
    if not all_trs:
        return None

    # Split into header row and data rows
    if thead:
        header_tr = thead.find("tr")
        data_trs = [tr for tr in all_trs if tr is not header_tr]
    else:
        header_tr = all_trs[0]
        data_trs = list(all_trs[1:])

    def cell_text(cell) -> str:
        return cell.get_text(strip=True).replace("|", "\\|")

    headers = [cell_text(c) for c in header_tr.find_all(["th", "td"])]
    if not headers:
        return None

    rows: list[list[str]] = []
    for tr in data_trs:
        cells = [cell_text(c) for c in tr.find_all(["td", "th"])]
        if cells:
            rows.append(cells)

    # Reject inconsistent column counts
    n_cols = len(headers)
    if any(len(row) != n_cols for row in rows):
        return None

    return _render_md_table(headers, rows, table_format)


def prune_tables(md_with_html: str, table_format: str = "llm") -> str:
    """Replace HTML tables in a Markdown string with cleaned HTML or pure Markdown.

    For each table:
    - If convertible to Markdown without semantic loss → replace with MD pipe table.
    - Otherwise → replace with a cleaned, minimal HTML table.

    Args:
        md_with_html: Markdown string containing embedded HTML tables.
        table_format: ``"human"`` for aligned columns, ``"llm"`` for compact tokens.
    """
    table_pattern = re.compile(r"<table.*?>.*?</table>", re.DOTALL | re.IGNORECASE)

    def _replace(m: re.Match) -> str:
        raw_html = m.group(0)
        # Always clean first (strip non-semantic attrs, normalize whitespace)
        cleaned = clean_html_table(raw_html)
        md = try_html_table_to_md(cleaned, table_format)
        return md if md is not None else cleaned

    return table_pattern.sub(_replace, md_with_html)
