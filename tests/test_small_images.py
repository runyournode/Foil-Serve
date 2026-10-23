"""Small images (below `small_image_threshold`) in the OCR-path Markdown.

They are never described. With `ignore_small_images` they leave no trace in the
Markdown; otherwise they are referenced by a bare <img>, plus their OCR only when
it is short enough to be trusted.
"""

from postprocessing import (
    SMALL_IMAGE_MAX_OCR_CHARS,
    collapse_blank_lines,
    reformat_md,
)

BIG = "imgs/big.jpg"
SMALL = "imgs/small.jpg"


def _block(src: str, ocr: str = "") -> str:
    return f'<div style="text-align: center;"><img src="{src}" alt="Image" width="50%" />{ocr}</div>'


def _md(small_ocr: str = "") -> str:
    return (
        f"Before\n\n{_block(BIG, 'Chart text')}\n\n{_block(SMALL, small_ocr)}\n\nAfter"
    )


def _reformat(md: str, ignore: bool, include_ocr: bool = True, ocr: str = "") -> str:
    return reformat_md(
        md=md,
        descriptions_dict={BIG: "A chart."},
        ocr_dict={BIG: "Chart text", SMALL: ocr},
        include_ocr=include_ocr,
        small_images={SMALL},
        ignore_small_images=ignore,
    )


class TestIgnored:
    def test_block_is_removed(self):
        out = _reformat(_md("Logo"), ignore=True, ocr="Logo")
        assert SMALL not in out
        assert "Logo" not in out
        assert out.count("<figure>") == 1, "the big image is untouched"

    def test_no_blank_line_left_behind(self):
        out = _reformat(f"Lieu\n\n{_block(SMALL)}\n\n\n06410 Biot", ignore=True)
        assert out == "Lieu\n\n06410 Biot"

    def test_big_image_keeps_its_figure(self):
        out = _reformat(_md(), ignore=True)
        assert f'<figure>\n<img src="{BIG}">\n' in out
        assert "<figcaption>  \nA chart.  \n</figcaption>" in out


class TestReferenced:
    def test_bare_img_without_caption(self):
        out = _reformat(_md(), ignore=False, include_ocr=False)
        assert f'\n<img src="{SMALL}">\n' in out
        assert out.count("<figure>") == 1
        assert out.count("<figcaption>") == 1, "only the big image is described"

    def test_short_ocr_is_kept(self):
        out = _reformat(_md("ACME"), ignore=False, ocr="ACME")
        assert f'<img src="{SMALL}">\n<ocr>  \nACME  \n</ocr>' in out

    def test_long_ocr_is_dropped(self):
        long_ocr = "x" * SMALL_IMAGE_MAX_OCR_CHARS
        out = _reformat(_md(long_ocr), ignore=False, ocr=long_ocr)
        assert f'<img src="{SMALL}">\n\nAfter' in out
        assert long_ocr not in out

    def test_empty_ocr_adds_no_tag(self):
        out = _reformat(_md(), ignore=False)
        assert f'<img src="{SMALL}">\n\nAfter' in out

    def test_ocr_off(self):
        out = _reformat(_md("ACME"), ignore=False, include_ocr=False, ocr="ACME")
        assert "ACME" not in out


def test_empty_ocr_on_a_figure_adds_no_tag():
    out = reformat_md(
        md=_block(BIG),
        descriptions_dict={BIG: "A chart."},
        ocr_dict={BIG: ""},
        include_ocr=True,
    )
    assert "<ocr>" not in out
    assert (
        out
        == f'<figure>\n<img src="{BIG}">\n<figcaption>  \nA chart.  \n</figcaption>\n</figure>'
    )


def test_undescribed_image_gets_no_placeholder():
    """An image missing from the descriptions has no caption, not a fake one."""
    out = reformat_md(
        md=_block(BIG),
        descriptions_dict={},
        ocr_dict={BIG: ""},
        include_ocr=False,
    )
    assert out == f'<figure>\n<img src="{BIG}">\n</figure>'
    assert "Too small" not in out


class TestCollapseBlankLines:
    def test_runs_become_one_blank_line(self):
        assert collapse_blank_lines("a\n\n\n\nb\n  \n\t\n\nc") == "a\n\nb\n\nc"

    def test_single_blank_line_and_hard_breaks_are_kept(self):
        md = "a  \nb\n\nc"
        assert collapse_blank_lines(md) == md

    def test_fenced_code_is_untouched(self):
        code = "```python\nx = 1\n\n\n\ny = 2\n```"
        md = f"a\n\n\n{code}\n\n\n\nb"
        assert collapse_blank_lines(md) == f"a\n\n{code}\n\nb"
