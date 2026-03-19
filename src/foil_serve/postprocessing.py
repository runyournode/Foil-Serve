import re
import logging

from bs4 import BeautifulSoup

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


def prune_tables(md_with_html: str) -> str:
    """
    Extract the HTML tables (from the md string) and simplify them.
    Reduce len (x3 - x5) without losing semantic.
    """
    table_pattern = re.compile(r"<table.*?>.*?</table>", re.DOTALL | re.IGNORECASE)
    return table_pattern.sub(lambda m: clean_html_table(m.group(0)), md_with_html)


def clean_html_table(html_table: str) -> str:
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
