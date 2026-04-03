import re
import logging

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
