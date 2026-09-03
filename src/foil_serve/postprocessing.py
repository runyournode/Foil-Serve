import re
import logging
from collections.abc import Sequence

from table_utils import prune_tables

logger = logging.getLogger(__name__)


def extract_raw_ocr(md_with_html: str) -> dict[str, str]:
    """
    Extract the ocr in the div img (from the md string) and create a dict {img_src: ocr_text}
    """
    pattern = re.compile(
        r'<div[^>]*>\s*<img src="([^"]+)"[^>]*>\s*(.*?)\s*</div>', re.DOTALL
    )
    ocr_results = {}
    for match in pattern.finditer(md_with_html):
        img_src = match.group(1)
        raw_text = match.group(2).strip()
        if "image is too blurry to recognize" in raw_text.lower():
            raw_text = ""
        ocr_results[img_src] = raw_text
    return ocr_results


def reformat_md(
    md: str,
    descriptions_dict: dict[str, str] | None,
    ocr_dict: dict[str, str],
    include_ocr: bool = True,
) -> str:
    """
    Reformat the md: simplify the img div, add ocr and desc in <figure>.
    If include_ocr is False, <ocr> tags are omitted from the output.
    """
    pattern = re.compile(
        r'<div[^>]*>\s*<img src="([^"]+)"[^>]*>\s*(.*?)\s*</div>', re.DOTALL
    )

    def replacement_logic(match):
        img_src = match.group(1)
        if descriptions_dict is not None:
            desc_val = descriptions_dict.get(
                img_src, "Too small for description."
            ).strip()
        else:  # image_description_model = None -> dict is None
            desc_val = ""

        parts = [
            "<figure>\n",
            f'<img src="{img_src}">\n',
        ]
        if desc_val:
            parts.append(f"<figcaption>  \n{desc_val}  \n</figcaption>\n")
        if include_ocr:
            ocr_val = ocr_dict.get(img_src, "").strip()
            parts.append(f"<ocr>  \n{ocr_val}  \n</ocr>\n")
        parts.append("</figure>")
        return "".join(parts)

    return pattern.sub(replacement_logic, md)


# ---------------------------------------------------------------------------
#  Per-page helpers
# ---------------------------------------------------------------------------
# The pipeline returns one Markdown string per PDF page. Post-processing runs
# page by page so page boundaries — and therefore output line numbers — stay
# locatable for the spreadsheet table of contents. Paddle never emits a table or
# a figure across a page boundary, so this is equivalent to processing the whole
# document at once.

PAGE_SEPARATOR = "  \n"


def join_pages(pages: Sequence[str]) -> str:
    """Rebuild the whole-document Markdown from per-page Markdown."""
    return PAGE_SEPARATOR.join(pages)


def prune_pages(pages: Sequence[str], table_format: str) -> list[str]:
    """prune_tables applied page by page."""
    return [
        prune_tables(md_with_html=page, table_format=table_format) for page in pages
    ]


def reformat_pages(
    pages: Sequence[str],
    descriptions_dict: dict[str, str] | None,
    ocr_dict: dict[str, str],
    include_ocr: bool,
) -> list[str]:
    """reformat_md applied page by page."""
    return [
        reformat_md(
            md=page,
            descriptions_dict=descriptions_dict,
            ocr_dict=ocr_dict,
            include_ocr=include_ocr,
        )
        for page in pages
    ]
