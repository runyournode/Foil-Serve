import re
import logging
from collections.abc import Collection, Mapping, Sequence

from PIL.Image import Image

from table_utils import prune_tables
from utils import EncodedImage

logger = logging.getLogger(__name__)

# OCR on a small image is kept only below this length (see reformat_md)
SMALL_IMAGE_MAX_OCR_CHARS = 40


def name_by_content(
    md: str, images: Mapping[str, Image]
) -> tuple[str, dict[str, EncodedImage]]:
    """Encode one page's crops and rename them after their content.

    PaddleX names a crop after its box only (no page number): two different images
    at the same place on two pages would share a name. Content names fix that, and
    make identical crops collapse into one image. References are rewritten in their
    quoted form, so a name that merely contains another one is left alone.
    """
    encoded: dict[str, EncodedImage] = {}
    for old_name, img in images.items():
        image = EncodedImage.from_pil(img)
        encoded[image.name] = image
        md = md.replace(f'"{old_name}"', f'"{image.name}"')
    return md, encoded


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


_BLANK_RUN = re.compile(r"\n(?:[ \t]*\n){2,}")
_LEADING_BLANK_LINES = re.compile(r"\A(?:[ \t]*\n)+")
_CODE_FENCE = re.compile(r"(?m)^(```|~~~).*?^\1[ \t]*$", re.DOTALL)


def collapse_blank_lines(md: str) -> str:
    """Keep at most one blank line in a row, outside fenced code blocks.

    Paddle often emits several blank lines between blocks, and a removed figure
    block leaves its surrounding ones behind: neither carries any meaning.
    """
    parts: list[str] = []
    last = 0
    for fence in _CODE_FENCE.finditer(md):
        parts.append(_BLANK_RUN.sub("\n\n", md[last : fence.start()]))
        parts.append(fence.group(0))
        last = fence.end()
    parts.append(_BLANK_RUN.sub("\n\n", md[last:]))
    return "".join(parts)


def reformat_md(
    md: str,
    descriptions_dict: dict[str, str] | None,
    ocr_dict: dict[str, str],
    include_ocr: bool = True,
    small_images: Collection[str] = (),
    ignore_small_images: bool = True,
) -> str:
    """
    Reformat the md: simplify the img div, add ocr and desc in <figure>.
    If include_ocr is False, <ocr> tags are omitted from the output; an empty OCR
    never produces an <ocr> tag.
    Runs of blank lines are collapsed to one (see collapse_blank_lines).

    Images in `small_images` (below `small_image_threshold`) are never described:
    - ignore_small_images=True: the whole block is removed from the Markdown.
    - ignore_small_images=False: a bare <img> reference, plus its <ocr> only when
      include_ocr is True and the OCR is shorter than SMALL_IMAGE_MAX_OCR_CHARS
      (longer text on a small crop is most likely a Paddle hallucination).
    """
    pattern = re.compile(
        r'<div[^>]*>\s*<img src="([^"]+)"[^>]*>\s*(.*?)\s*</div>', re.DOTALL
    )

    def replacement_logic(match):
        img_src = match.group(1)
        if img_src in small_images:
            if ignore_small_images:
                return ""
            parts = [f'<img src="{img_src}">']
            ocr_val = ocr_dict.get(img_src, "").strip()
            if include_ocr and 0 < len(ocr_val) < SMALL_IMAGE_MAX_OCR_CHARS:
                parts.append(f"\n<ocr>  \n{ocr_val}  \n</ocr>")
            return "".join(parts)

        if descriptions_dict is not None:
            desc_val = descriptions_dict.get(img_src, "").strip()
        else:  # image_description_model = None -> dict is None
            desc_val = ""

        parts = [
            "<figure>\n",
            f'<img src="{img_src}">\n',
        ]
        if desc_val:
            parts.append(f"<figcaption>  \n{desc_val}  \n</figcaption>\n")
        ocr_val = ocr_dict.get(img_src, "").strip()
        if include_ocr and ocr_val:  # never an empty <ocr> tag
            parts.append(f"<ocr>  \n{ocr_val}  \n</ocr>\n")
        parts.append("</figure>")
        return "".join(parts)

    return collapse_blank_lines(pattern.sub(replacement_logic, md))


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


def join_marked_pages(pages: Sequence[str]) -> str:
    """Like join_pages, but each page starts with `<page number="n/n_max" />`.

    The marker is its own HTML block (one blank line around it), so it never merges
    into the previous page's last paragraph. Blank lines at the edges of a page are
    dropped. An empty page keeps its marker.
    """
    total = len(pages)
    marked = []
    for n, page in enumerate(pages, start=1):
        body = _LEADING_BLANK_LINES.sub("", page).rstrip()
        marker = f'<page number="{n}/{total}" />'
        marked.append(f"{marker}\n\n{body}" if body else marker)
    return "\n\n".join(marked)


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
    small_images: Collection[str] = (),
    ignore_small_images: bool = True,
) -> list[str]:
    """reformat_md applied page by page."""
    return [
        reformat_md(
            md=page,
            descriptions_dict=descriptions_dict,
            ocr_dict=ocr_dict,
            include_ocr=include_ocr,
            small_images=small_images,
            ignore_small_images=ignore_small_images,
        )
        for page in pages
    ]
